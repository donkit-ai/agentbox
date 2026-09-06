"""Single-use browser capabilities. Passwords never cross the agent/tool boundary."""
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.auth import Principal, require_scope
from agentbox.api.deps import get_runtime, get_session
from agentbox.api.errors import APIError, not_found
from agentbox.api.schemas import Model
from agentbox.connectors import oauth
from agentbox.connectors.delivery import managed_delivery_ready
from agentbox.connectors.service import PRESETS, connect_mailbox
from agentbox.connectors.views import BrowserRoute, page
from agentbox.db.models import ConnectionLink, Organization, utcnow
from agentbox.domain.ids import new_id
from agentbox.runtime import Runtime
from agentbox.security.crypto import decrypt_json, encrypt_json
from agentbox.services.inboxes import create_inbox

router = APIRouter(tags=["connections"], route_class=BrowserRoute)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()



class LinkCreate(Model):
    provider: str = Field(default="gmail", max_length=30)


async def make_link(session, runtime, organization_id, provider):
    if provider not in PRESETS:
        raise APIError(422, "validation_error", "Unknown mailbox provider.")
    public = urlsplit(runtime.settings.public_base_url)
    if public.scheme != "https" and public.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise APIError(409, "https_required", "Set AGENTBOX_PUBLIC_BASE_URL to an HTTPS URL before collecting mailbox credentials.")
    token = secrets.token_urlsafe(32)
    row = ConnectionLink(id=new_id("tok"), organization_id=organization_id, provider=provider,
        token_hash=digest(token), expires_at=utcnow() + timedelta(minutes=15),
        state_encrypted=encrypt_json(runtime.settings.app_secret_key, {"csrf": secrets.token_urlsafe(32)}))
    session.add(row)
    await session.commit()
    return {"id": row.id, "url": f"{runtime.settings.public_base_url.rstrip('/')}/connect/{row.id}#{token}",
            "status": row.status, "expires_at": row.expires_at.isoformat()}


@router.post("/v1/connection-links", status_code=201)
async def create_link(body: LinkCreate, principal: Principal = Depends(require_scope("inboxes:write")),
                      session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    return await make_link(session, runtime, principal.organization_id, body.provider)


@router.get("/v1/connection-links/{link_id}")
async def link_status(link_id: str, principal: Principal = Depends(require_scope("inboxes:write")),
                       session: AsyncSession = Depends(get_session)):
    row = await session.scalar(select(ConnectionLink).where(ConnectionLink.id == link_id,
                                ConnectionLink.organization_id == principal.organization_id))
    if row is None:
        raise not_found("connection_link", link_id)
    return {"id": row.id, "status": "expired" if row.status == "pending" and row.expires_at <= utcnow() else row.status,
            "inbox_id": row.inbox_id, "expires_at": row.expires_at.isoformat()}


async def browser_link(session, link_id, request, *, token=None):
    row = await session.scalar(select(ConnectionLink).where(ConnectionLink.id == link_id).with_for_update())
    token = token or request.cookies.get("ab_connect_" + link_id, "")
    if row is None or not secrets.compare_digest(row.token_hash, digest(token)):
        raise APIError(404, "not_found", "Connection link not found. Open the full link supplied by your agent.")
    if row.status != "pending" or row.expires_at <= utcnow():
        raise APIError(410, "link_expired", "This link is expired or already used. Ask for a new connection link.")
    org = await session.get(Organization, row.organization_id)
    if org is None or org.status != "active":
        raise APIError(403, "forbidden", "Organization is not active.")
    return row


class BrowserToken(Model):
    token: str = Field(min_length=20, max_length=100)


@router.post("/connect/{link_id}/session", include_in_schema=False)
async def exchange_link(link_id: str, body: BrowserToken, request: Request,
                         session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    await browser_link(session, link_id, request, token=body.token)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.set_cookie("ab_connect_" + link_id, body.token, httponly=True, samesite="lax", max_age=900,
                        secure=runtime.settings.public_base_url.startswith("https:"), path="/connect/")
    return response


@router.get("/connect/{link_id}", include_in_schema=False)
async def connect_page(link_id: str, request: Request, session: AsyncSession = Depends(get_session),
                         runtime: Runtime = Depends(get_runtime)):
    if not request.cookies.get("ab_connect_" + link_id):
        return page("opening")
    row = await browser_link(session, link_id, request)
    state = decrypt_json(runtime.settings.app_secret_key, row.state_encrypted)
    org = await session.get(Organization, row.organization_id)
    provider = "m365" if row.provider == "m365" else "gmail"
    return page("choice", link=row, csrf=state["csrf"], organization=org.name,
                self_hosted=runtime.settings.edition == "oss",
                managed_ready=await managed_delivery_ready(session, runtime.settings, row.organization_id),
                managed_domain=runtime.settings.managed_domain, oauth_ready=oauth.configured(runtime.settings, provider),
                oauth_provider=provider, preset=PRESETS[row.provider],
                advanced=runtime.settings.edition == "oss" and runtime.settings.allow_app_password_connections)


async def form_state(session, runtime, link_id, request):
    row = await browser_link(session, link_id, request)
    state = decrypt_json(runtime.settings.app_secret_key, row.state_encrypted)
    form = await request.form(max_fields=10, max_files=0, max_part_size=16384)
    if not secrets.compare_digest(str(form.get("csrf", "")), state["csrf"]):
        raise APIError(403, "csrf_invalid", "Reload the connection form and try again.")
    return row, state, form


async def finish(session, runtime, row, **kwargs):
    inbox, _ = await connect_mailbox(session, runtime.settings, organization_id=row.organization_id,
        provider=kwargs.pop("provider", row.provider), username=kwargs["address"], verify_credentials=True, http=runtime.http, **kwargs)
    row.status, row.inbox_id = "connected", inbox.id
    row.state_encrypted = encrypt_json(runtime.settings.app_secret_key, {})
    await session.commit()
    return page("success", address=inbox.address, managed=False,
                gmail=kwargs.get("oauth", {}).get("transport") == "gmail_api")


@router.post("/connect/{link_id}/managed", include_in_schema=False)
async def connect_managed(link_id: str, request: Request, session: AsyncSession = Depends(get_session),
                          runtime: Runtime = Depends(get_runtime)):
    row, _, form = await form_state(session, runtime, link_id, request)
    if not await managed_delivery_ready(session, runtime.settings, row.organization_id):
        raise APIError(409, "managed_delivery_unavailable", "External delivery is not configured.")
    inbox = await create_inbox(session, settings=runtime.settings, organization_id=row.organization_id,
                               username=str(form.get("username", "")).strip() or None)
    row.status, row.inbox_id = "connected", inbox.id
    row.state_encrypted = encrypt_json(runtime.settings.app_secret_key, {})
    await session.commit()
    return page("success", address=inbox.address, managed=True)


@router.post("/connect/{link_id}/password", include_in_schema=False)
async def connect_password(link_id: str, request: Request, session: AsyncSession = Depends(get_session),
                             runtime: Runtime = Depends(get_runtime)):
    row, _, form = await form_state(session, runtime, link_id, request)
    try:
        return await finish(session, runtime, row, address=str(form.get("address", "")), password=str(form.get("password", "")),
            imap_host=form.get("imap_host"), smtp_host=form.get("smtp_host"),
            imap_port=int(form["imap_port"]) if form.get("imap_port") else None,
            smtp_port=int(form["smtp_port"]) if form.get("smtp_port") else None,
            import_history=form.get("import_history") == "on")
    except ValueError as exc:
        raise APIError(422, "validation_error", "Ports must be valid integers.") from exc


@router.post("/connect/{link_id}/oauth", include_in_schema=False)
async def start_oauth(link_id: str, request: Request, session: AsyncSession = Depends(get_session),
                       runtime: Runtime = Depends(get_runtime)):
    row, state, _ = await form_state(session, runtime, link_id, request)
    provider = "m365" if row.provider == "m365" else "gmail"
    state["oauth_provider"] = provider
    state.update(verifier=secrets.token_urlsafe(48), nonce=secrets.token_urlsafe(32), oauth_state=secrets.token_urlsafe(32))
    redirect_uri = runtime.settings.public_base_url.rstrip("/") + "/connect/oauth/callback"
    url = oauth.authorize_url(runtime.settings, provider, redirect_uri, row.id + "." + state["oauth_state"],
                              state["verifier"], state["nonce"])
    row.state_encrypted = encrypt_json(runtime.settings.app_secret_key, state)
    await session.commit()
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/connect/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "",
                          session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)):
    link_id, _, token = state.partition(".")
    row = await browser_link(session, link_id, request)
    saved = decrypt_json(runtime.settings.app_secret_key, row.state_encrypted)
    if not token or not secrets.compare_digest(token, saved.get("oauth_state", "")):
        raise APIError(403, "oauth_state_invalid", "OAuth state does not match this browser session.")
    # Consume each OAuth attempt, including cancellations. The link remains retryable.
    saved.pop("oauth_state", None)
    row.state_encrypted = encrypt_json(runtime.settings.app_secret_key, saved)
    await session.flush()
    if error or not code:
        await session.commit()
        return page("error", title="Подключение отменено", detail="Доступ к почте не подключён. Вы можете выбрать другой вариант или повторить вход.",
                    retry_url=f"/connect/{row.id}")
    redirect_uri = runtime.settings.public_base_url.rstrip("/") + "/connect/oauth/callback"
    tokens = await oauth.exchange_code(runtime.http, runtime.settings, saved["oauth_provider"], code, redirect_uri,
                                       saved["verifier"], saved["nonce"])
    return await finish(session, runtime, row, provider=saved["oauth_provider"], address=tokens["email"], password=None, oauth=tokens, import_history=False)
