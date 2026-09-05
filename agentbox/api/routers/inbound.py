"""Inbound email webhooks, separate from outbound delivery event webhooks."""

import base64
import hashlib
import hmac
import json
import secrets
from typing import Literal

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError, not_found
from agentbox.db.models import InboundEndpoint, InboundIngest, Inbox, Organization, utcnow
from agentbox.domain.ids import new_id
from agentbox.governance.policies import get_effective_policy
from agentbox.inbound.forms import mime_form
from agentbox.inbound.providers import InboundEmail, parse_postmark, valid_timestamp, verify_mailgun_signature
from agentbox.jobs.queue import enqueue
from agentbox.providers.sendgrid import verify_sendgrid_signature
from agentbox.runtime import Runtime
from agentbox.security.crypto import decrypt_json, encrypt_json
from agentbox.services.events import emit

router = APIRouter(prefix="/v1", tags=["inbound"])


class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["mailgun", "sendgrid", "postmark"]
    name: str = Field(default="Inbound email", min_length=1, max_length=100)
    signing_key: SecretStr | None = None
    public_key: str | None = Field(default=None, max_length=4096)


def endpoint_to_dict(endpoint: InboundEndpoint, runtime: Runtime) -> dict:
    suffix = "/mime" if endpoint.provider == "mailgun" else ""
    return {
        "id": endpoint.id, "name": endpoint.name, "provider": endpoint.provider,
        "url": f"{runtime.settings.api_base_url.rstrip('/')}/v1/inbound/{endpoint.provider}/{endpoint.id}{suffix}",
        "authentication": "basic" if endpoint.provider == "postmark" else "signature",
        "created_at": endpoint.created_at.isoformat(),
    }


@router.post("/inbound-endpoints", status_code=201)
async def create_endpoint(body: EndpointCreate, principal: Principal = Depends(require_scope("providers:write")),
                          session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    cfg = {}
    if body.provider == "mailgun":
        key = body.signing_key.get_secret_value() if body.signing_key else ""
        if not key.strip() or len(key) > 4096 or body.public_key is not None:
            raise APIError(422, "validation_error", "Mailgun requires its HTTP webhook signing_key.")
        cfg["signing_key"] = key
    elif body.provider == "sendgrid":
        try:
            key = load_der_public_key(base64.b64decode(body.public_key or "", validate=True))
            if not isinstance(key, ec.EllipticCurvePublicKey) or body.signing_key is not None:
                raise ValueError("Invalid key")
        except (ValueError, TypeError) as exc:
            raise APIError(422, "validation_error", "SendGrid requires its Inbound Parse security policy public_key.") \
                from exc
        cfg["public_key"] = body.public_key
    else:
        if body.public_key is not None or body.signing_key is not None:
            raise APIError(422, "validation_error", "Postmark uses generated HTTP Basic credentials.")
        cfg["password"] = secrets.token_urlsafe(32)
    endpoint = InboundEndpoint(
        id=new_id("iep"), organization_id=principal.organization_id, provider=body.provider, name=body.name,
        config_encrypted=encrypt_json(runtime.settings.app_secret_key, cfg),
    )
    session.add(endpoint)
    await session.flush()
    await emit(session, organization_id=principal.organization_id, resource_type="inbound_endpoint",
               resource_id=endpoint.id, type="inbound_endpoint.created",
               payload={"provider": endpoint.provider, "actor": principal.api_key_id})
    await session.commit()
    result = endpoint_to_dict(endpoint, runtime)
    if endpoint.provider == "postmark":
        result["credentials"] = {"username": endpoint.id, "password": cfg["password"]}
    return result


@router.get("/inbound-endpoints")
async def list_endpoints(principal: Principal = Depends(require_scope("providers:read")),
                         session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    rows = await session.scalars(select(InboundEndpoint)
                                 .where(InboundEndpoint.organization_id == principal.organization_id)
                                 .order_by(InboundEndpoint.id))
    return {"data": [endpoint_to_dict(row, runtime) for row in rows], "next_cursor": None}


@router.delete("/inbound-endpoints/{endpoint_id}", status_code=204, response_class=Response)
async def delete_endpoint(endpoint_id: str, principal: Principal = Depends(require_scope("providers:write")),
                           session: AsyncSession = Depends(get_session)):
    endpoint = await session.scalar(select(InboundEndpoint).where(
        InboundEndpoint.id == endpoint_id, InboundEndpoint.organization_id == principal.organization_id,
    ))
    if endpoint is None:
        raise not_found("inbound_endpoint", endpoint_id)
    await session.delete(endpoint)
    await emit(session, organization_id=principal.organization_id, resource_type="inbound_endpoint",
               resource_id=endpoint.id, type="inbound_endpoint.deleted", payload={"actor": principal.api_key_id})
    await session.commit()
    return Response(status_code=204)


async def _bounded_body(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit:
        raise APIError(413, "message_too_large", "Inbound webhook request is too large.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise APIError(413, "message_too_large", "Inbound webhook request is too large.")
        body.extend(chunk)
    return bytes(body)


def _verify_basic(request: Request, endpoint: InboundEndpoint, password: str) -> None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    try:
        supplied = base64.b64decode(value, validate=True) if scheme.lower() == "basic" else b""
    except ValueError:
        supplied = b""
    expected = f"{endpoint.id}:{password}".encode()
    if not hmac.compare_digest(supplied, expected):
        raise APIError(401, "unauthorized", "Invalid inbound webhook credentials.")


async def _parse(request: Request, endpoint: InboundEndpoint, cfg: dict, max_bytes: int) -> InboundEmail:
    if endpoint.provider == "postmark":
        _verify_basic(request, endpoint, cfg["password"])
    # URL encoding can triple MIME size; JSON/base64 and multipart add overhead.
    limit = max_bytes * 3 + 1024 * 1024
    body = await _bounded_body(request, limit)
    if endpoint.provider == "sendgrid":
        ts = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")
        sig = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
        if not valid_timestamp(ts) or not verify_sendgrid_signature(cfg["public_key"], sig, ts, body):
            raise APIError(401, "unauthorized", "Invalid SendGrid inbound signature.")
    try:
        if endpoint.provider == "postmark":
            return parse_postmark(body, max_bytes)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"multipart/form-data", "application/x-www-form-urlencoded"}:
            raise APIError(415, "unsupported_media_type", "Expected a form containing the raw MIME message.")
        field = "body-mime" if endpoint.provider == "mailgun" else "email"
        async with mime_form(request.headers, body, field, limit) as (form, raw):
            if endpoint.provider == "mailgun":
                fields = [form.get(name, "") for name in ("timestamp", "token", "signature")]
                if not all(isinstance(value, str) for value in fields) or not verify_mailgun_signature(
                    cfg["signing_key"], *fields,
                ):
                    raise APIError(401, "unauthorized", "Invalid Mailgun inbound signature.")
                mail_from, recipients = form.get("sender", ""), [form.get("recipient", "")]
            else:
                envelope = json.loads(form.get("envelope", "{}"))
                if not isinstance(envelope, dict):
                    raise ValueError("Invalid envelope")
                mail_from, recipients = envelope.get("from", ""), envelope.get("to", [])
            if isinstance(raw, UploadFile):
                raw = await raw.read(max_bytes + 1)
            if not raw:
                raise APIError(422, "validation_error", "Raw MIME is required: Mailgun URL must end in /mime; "
                               "enable SendGrid 'POST the raw, full MIME message' (send_raw=true).")
            return InboundEmail(raw, mail_from, recipients)
    except (ValueError, TypeError, MultiPartException) as exc:
        raise APIError(422, "validation_error", "Malformed inbound email payload.") from exc
    except OverflowError as exc:
        raise APIError(413, "message_too_large", "Inbound message is too large.") from exc


@router.post("/inbound/{provider}/{endpoint_id}/mime", include_in_schema=False)
@router.post("/inbound/{provider}/{endpoint_id}")
async def receive(provider: str, endpoint_id: str, request: Request, session: AsyncSession = Depends(get_session),
                  runtime: Runtime = Depends(get_runtime)):
    endpoint = await session.scalar(select(InboundEndpoint).join(Organization).where(
        InboundEndpoint.id == endpoint_id, InboundEndpoint.provider == provider, Organization.status == "active",
    ))
    if endpoint is None:
        raise APIError(404, "not_found", "Unknown inbound endpoint.")
    cfg = decrypt_json(runtime.settings.app_secret_key, endpoint.config_encrypted)
    email = await _parse(request, endpoint, cfg, runtime.settings.max_inbound_bytes)
    if len(email.raw) > runtime.settings.max_inbound_bytes:
        raise APIError(413, "message_too_large", "Inbound message is too large.")
    if (not isinstance(email.mail_from, str) or len(email.mail_from) > 320
            or not isinstance(email.recipients, list) or not 1 <= len(email.recipients) <= 100
            or any(not isinstance(r, str) or not 3 <= len(r) <= 320 or "@" not in r for r in email.recipients)):
        raise APIError(422, "validation_error", "Invalid SMTP envelope.")
    recipients = sorted({r.strip().lower() for r in email.recipients})
    digest = email.delivery_id or hashlib.sha256(email.raw).hexdigest()
    accepted = duplicates = ignored = 0
    for recipient in recipients:
        # Use the provider's envelope, never MIME To/Cc (which can name unrelated
        # inboxes). Every target must belong to this endpoint's organization.
        inbox = await session.scalar(select(Inbox).where(
            Inbox.address == recipient, Inbox.organization_id == endpoint.organization_id,
            Inbox.deleted_at.is_(None), Inbox.status == "active", Inbox.provider_mode == "managed",
        ))
        if inbox is None or (inbox.expires_at is not None and inbox.expires_at <= utcnow()):
            ignored += 1
            continue
        policy = await get_effective_policy(session, endpoint.organization_id, inbox.id)
        if not policy.get("receive_enabled", True):
            ignored += 1
            continue
        dedupe = hashlib.sha256(json.dumps([endpoint.id, recipient, digest]).encode()).hexdigest()
        ingest_id = new_id("ing")
        storage_key = f"org/{endpoint.organization_id}/raw/{ingest_id}.eml"
        inserted = await session.scalar(insert(InboundIngest).values(
            id=ingest_id, organization_id=endpoint.organization_id, kind="message", inbox_id=inbox.id,
            storage_key=storage_key, mail_from=email.mail_from.strip().lower(), rcpt_to=recipient,
            size_bytes=len(email.raw), status="received", dedupe_key=dedupe,
        ).on_conflict_do_nothing(constraint="uq_inbound_ingests_dedupe_key").returning(InboundIngest.id))
        if inserted is None:
            duplicates += 1
            continue
        await runtime.storage.put_bytes(storage_key, email.raw, "message/rfc822")
        await enqueue(session, "inbound_process", {"ingest_id": ingest_id})
        accepted += 1
    # Acknowledge only after both durable storage and the job transaction succeed.
    # Permanent recipient failures are acknowledged to avoid endless retries.
    await session.commit()
    return {"accepted": accepted, "duplicates": duplicates, "ignored": ignored}
