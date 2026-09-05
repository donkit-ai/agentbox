"""Short requests for long-lived work. State survives both agent and server restarts."""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Path
from pydantic import Field
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_session
from agentbox.api.errors import APIError, not_found
from agentbox.api.schemas import Model
from agentbox.db.models import Event, EventReceipt, Message, ReplyWait, utcnow
from agentbox.services.events import event_to_dict
from agentbox.services.messages import get_message

router = APIRouter(prefix="/v1", tags=["workflows"])


class WaitCreate(Model):
    expires_in: int = Field(default=86400, ge=1, le=604800)


async def resolve_wait(session, original, wait):
    if wait.status == "matched" and wait.reply_message_id is None:
        wait.status = "expired"  # reply was removed by retention
        await session.commit()
    if wait.status == "pending":
        # A subject/thread match alone is insufficient: replies must reference this outgoing Message-ID.
        reply = await session.scalar(select(Message).where(
            Message.organization_id == original.organization_id, Message.inbox_id == original.inbox_id,
            Message.direction == "inbound", Message.created_at >= original.created_at,
            Message.created_at <= wait.expires_at,
            or_(Message.in_reply_to == original.internet_message_id,
                Message.references.contains([original.internet_message_id])),
        ).order_by(Message.created_at, Message.id).limit(1))
        if reply:
            wait.status, wait.reply_message_id = "matched", reply.id
        elif utcnow() >= wait.expires_at:
            wait.status = "expired"
        await session.commit()
    return {"message_id": wait.message_id, "status": wait.status, "reply_message_id": wait.reply_message_id,
            "expires_at": wait.expires_at.isoformat()}


@router.post("/messages/{message_id}/reply-wait")
async def create_wait(message_id: str, body: WaitCreate,
                      principal: Principal = Depends(require_scope("messages:read")),
                      session: AsyncSession = Depends(get_session)):
    original = await get_message(session, principal.organization_id, message_id)
    if original.direction != "outbound":
        raise APIError(422, "validation_error", "A reply wait must reference an outgoing message.")
    await session.execute(insert(ReplyWait).values(message_id=message_id, organization_id=principal.organization_id,
        inbox_id=original.inbox_id, status="pending", expires_at=utcnow() + timedelta(seconds=body.expires_in),
        created_at=utcnow()).on_conflict_do_nothing())
    wait = await session.get(ReplyWait, message_id)
    await session.commit()
    return await resolve_wait(session, original, wait)


@router.get("/messages/{message_id}/reply-wait")
async def get_wait(message_id: str, principal: Principal = Depends(require_scope("messages:read")),
                   session: AsyncSession = Depends(get_session)):
    original = await get_message(session, principal.organization_id, message_id)
    wait = await session.get(ReplyWait, message_id)
    if wait is None:
        raise not_found("reply_wait", message_id)
    return await resolve_wait(session, original, wait)


class Claim(Model):
    limit: int = Field(default=10, ge=1, le=100)
    lease_seconds: int = Field(default=120, ge=10, le=3600)
    types: list[str] = Field(default_factory=lambda: ["message.received"], min_length=1, max_length=50)
    since: datetime | None = None


@router.post("/consumers/{consumer}/claim")
async def claim(body: Claim, consumer: str = Path(pattern=r"^[a-zA-Z0-9_.-]{1,100}$"),
                 principal: Principal = Depends(require_scope("events:consume")),
                 session: AsyncSession = Depends(get_session)):
    now = utcnow()
    stmt = select(Event).outerjoin(EventReceipt, and_(
        EventReceipt.event_id == Event.id, EventReceipt.organization_id == principal.organization_id,
        EventReceipt.consumer == consumer,
    )).where(Event.organization_id == principal.organization_id, Event.type.in_(body.types),
        EventReceipt.acknowledged_at.is_(None),
        or_(EventReceipt.event_id.is_(None), EventReceipt.leased_until <= now))
    if body.since:
        stmt = stmt.where(Event.created_at >= body.since)
    events = await session.scalars(stmt.order_by(Event.id).limit(body.limit)
                                  .with_for_update(of=Event, skip_locked=True))
    data = []
    for event in events:
        token, until = secrets.token_urlsafe(32), now + timedelta(seconds=body.lease_seconds)
        values = {"organization_id": principal.organization_id, "consumer": consumer, "event_id": event.id,
                  "api_key_id": principal.api_key_id, "lease_token": token, "leased_until": until}
        await session.execute(insert(EventReceipt).values(**values).on_conflict_do_update(
            index_elements=["organization_id", "consumer", "event_id"], set_=values))
        data.append({"event": event_to_dict(event), "lease_token": token, "leased_until": until.isoformat()})
    await session.commit()
    return {"data": data}


class Acknowledge(Model):
    lease_token: str = Field(min_length=1, max_length=64)


@router.post("/consumers/{consumer}/events/{event_id}/ack")
async def acknowledge(consumer: str, event_id: str, body: Acknowledge,
                       principal: Principal = Depends(require_scope("events:consume")),
                       session: AsyncSession = Depends(get_session)):
    # Check current visibility, including inbox restrictions, before touching a receipt.
    event = await session.scalar(select(Event).where(Event.id == event_id,
                                                     Event.organization_id == principal.organization_id))
    if event is None:
        raise not_found("event", event_id)
    receipt = await session.get(EventReceipt, (principal.organization_id, consumer, event_id), with_for_update=True)
    if (receipt is None or receipt.api_key_id != principal.api_key_id
            or not secrets.compare_digest(receipt.lease_token, body.lease_token)):
        raise APIError(409, "lease_conflict", "This event is not leased to this caller.")
    if receipt.acknowledged_at is None:
        if receipt.leased_until <= utcnow():
            raise APIError(409, "lease_expired", "Claim again before acknowledging an expired lease.")
        receipt.acknowledged_at = utcnow()
        await session.commit()
    return {"acknowledged": True, "event_id": event_id}
