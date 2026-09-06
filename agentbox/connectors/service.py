"""Existing mailboxes through Gmail API or IMAP/SMTP, with encrypted credentials."""

from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import ssl
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.errors import APIError
from agentbox.config import Settings
from agentbox.connectors.gmail import GmailAPI
from agentbox.connectors.xoauth2 import xoauth2_string
from agentbox.db.models import InboundIngest, Inbox, MailboxConnection, utcnow
from agentbox.domain.addresses import is_valid_email, normalize_email
from agentbox.domain.ids import new_id
from agentbox.extensions import registry
from agentbox.jobs.queue import enqueue
from agentbox.security.crypto import decrypt_json, encrypt_json
from agentbox.services.events import emit

log = structlog.get_logger("agentbox.connectors")

PRESETS: dict[str, dict] = {
    "yandex360": {"label": "Yandex 360 / Yandex Mail", "imap_host": "imap.yandex.ru", "imap_port": 993,
                  "smtp_host": "smtp.yandex.ru", "smtp_port": 465, "smtp_ssl": True,
                  "hint": "Enable IMAP (Settings → Mail clients) and create an app password (id.yandex.ru → Security)."},
    "vkworkspace": {"label": "VK WorkSpace / Mail.ru", "imap_host": "imap.mail.ru", "imap_port": 993,
                    "smtp_host": "smtp.mail.ru", "smtp_port": 465, "smtp_ssl": True,
                    "hint": "Create an app password for external clients."},
    "m365": {"label": "Microsoft 365 / Exchange Online", "imap_host": "outlook.office365.com", "imap_port": 993,
             "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_ssl": False,
             "hint": "Requires OAuth: Exchange Online does not support IMAP app passwords. The operator must register "
                     "a Microsoft app and enable IMAP and SMTP AUTH for the mailbox.", "oauth": "m365"},
    "gmail": {"label": "Google Workspace / Gmail", "imap_host": "imap.gmail.com", "imap_port": 993,
              "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_ssl": False,
              "hint": "Connect through Google. App passwords are an advanced self-hosting option.", "oauth": "gmail"},
    "imap": {"label": "Generic IMAP + SMTP", "imap_host": "", "imap_port": 993, "smtp_host": "", "smtp_port": 587,
             "smtp_ssl": False, "hint": "Any mailbox that speaks IMAP over TLS and authenticated SMTP."},
}


@dataclass
class FetchedMail:
    uid: int
    raw: bytes


class ImapClient(Protocol):
    def fetch_new(self, since_uid: int, limit: int = 50) -> list[FetchedMail]: ...

    def close(self) -> None: ...


class StdlibImap:
    def __init__(self, host: str, port: int, username: str, password: str | None, folder: str = "INBOX",
                 access_token: str | None = None) -> None:
        self.conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
        if access_token:
            self.conn.authenticate("XOAUTH2", lambda _challenge: xoauth2_string(username, access_token).encode())
        else:
            self.conn.login(username, password or "")
        self.conn.select(folder, readonly=True)

    def fetch_new(self, since_uid: int, limit: int = 50) -> list[FetchedMail]:
        typ, data = self.conn.uid("SEARCH", None, f"UID {since_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = [int(u) for u in data[0].split() if int(u) > since_uid][:limit]
        out = []
        for uid in uids:
            typ, parts = self.conn.uid("FETCH", str(uid), "(RFC822)")
            if typ == "OK" and parts and isinstance(parts[0], tuple):
                out.append(FetchedMail(uid=uid, raw=parts[0][1]))
        return out

    def latest_uid(self) -> int:
        typ, data = self.conn.response("UIDNEXT")
        if data and data[0]:
            return max(0, int(data[0]) - 1)
        typ, data = self.conn.uid("SEARCH", None, "ALL")
        return max((int(uid) for uid in data[0].split()), default=0) if typ == "OK" and data and data[0] else 0

    def close(self) -> None:
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _open_client(cfg: dict) -> ImapClient:
    return StdlibImap(cfg["imap_host"], int(cfg["imap_port"]), cfg["username"], cfg.get("password"), cfg.get("folder", "INBOX"),
                      access_token=cfg.get("access_token") if cfg.get("auth") == "oauth" else None)


# ---------------------------------------------------------------- CRUD

def connection_to_dict(c: MailboxConnection, settings: Settings) -> dict:
    cfg = decrypt_json(settings.app_secret_key, c.config_encrypted)
    return {"id": c.id, "inbox_id": c.inbox_id, "provider": c.provider, "address": c.address, "status": c.status,
            "imap_host": cfg.get("imap_host"), "smtp_host": cfg.get("smtp_host"), "username": cfg.get("username"),
            "auth": cfg.get("auth", "password"),
            "transport": cfg.get("transport", "imap_smtp"),
            "last_uid": c.last_uid, "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
            "last_error": c.last_error, "sync_interval_seconds": c.sync_interval_seconds}


async def connect_mailbox(
    session: AsyncSession, settings: Settings, *, organization_id: str, provider: str, address: str, username: str | None,
    password: str | None, imap_host: str | None = None, imap_port: int | None = None, smtp_host: str | None = None,
    smtp_port: int | None = None, smtp_ssl: bool | None = None, display_name: str | None = None, metadata: dict | None = None,
    oauth: dict | None = None, verify_credentials: bool = False, import_history: bool = True, http=None,
) -> tuple[Inbox, MailboxConnection]:
    """`oauth` (extension-provided token config) replaces the password; it is stored encrypted with the rest."""
    preset = PRESETS.get(provider)
    if preset is None:
        raise APIError(422, "validation_error", f"Unknown provider '{provider}'.")
    address = normalize_email(address)
    if not is_valid_email(address):
        raise APIError(422, "validation_error", "Enter the mailbox address.")
    if not password and not oauth:
        raise APIError(422, "validation_error", "Password / app password is required.")
    if not oauth and (settings.edition != "oss" or not settings.allow_app_password_connections):
        raise APIError(403, "app_password_disabled", "App passwords are disabled. Connect through the provider's authorization page.")
    if provider == "m365" and not oauth:
        raise APIError(422, "oauth_required", "Microsoft 365 requires OAuth; app passwords cannot authenticate IMAP.")
    for hook in registry().hooks(settings, "inbox.before_create"):
        await hook(session, organization_id, settings)
    cfg = {"imap_host": imap_host or preset["imap_host"], "imap_port": imap_port or preset["imap_port"],
           "smtp_host": smtp_host or preset["smtp_host"], "smtp_port": smtp_port or preset["smtp_port"],
           "smtp_ssl": preset["smtp_ssl"] if smtp_ssl is None else smtp_ssl, "username": username or address,
           "password": password, "folder": "INBOX"}
    if oauth:
        cfg.update({k: v for k, v in oauth.items() if k != "email"})
    gmail = provider == "gmail" and bool(oauth)
    if gmail:
        from agentbox.connectors.oauth import gmail_scopes

        if cfg.get("transport") != "gmail_api":
            raise APIError(409, "oauth_reconnect_required", "Reconnect Gmail to use limited read/send permissions.")
        gmail_scopes(" ".join(cfg.get("scopes", [])))
        if http is None:
            raise APIError(503, "gmail_unavailable", "Gmail requires the runtime HTTP client.")
        await GmailAPI(http, cfg).initialize(address, import_history=import_history)
        for field in ("imap_host", "imap_port", "smtp_host", "smtp_port", "smtp_ssl", "password", "folder"):
            cfg.pop(field, None)
    if not gmail and (not cfg["imap_host"] or not cfg["smtp_host"]):
        raise APIError(422, "validation_error", "IMAP and SMTP hosts are required for a generic connection.")
    initial_uid = 0
    if verify_credentials and not gmail:
        def check():
            client = _client_factory(cfg)
            try:
                return 0 if import_history else client.latest_uid()
            finally:
                client.close()
        try:
            initial_uid = await asyncio.to_thread(check)
        except Exception as exc:
            raise APIError(422, "mailbox_login_failed", "IMAP login failed. Check the address, app password, IMAP access and TLS host/port.") from exc
    existing = await session.scalar(select(Inbox).where(Inbox.address == address, Inbox.deleted_at.is_(None)))
    if existing is not None:
        raise APIError(409, "conflict", f"{address} is already connected.")
    from agentbox.db.models import Domain

    domain_name = address.rpartition("@")[2]
    domain = await session.scalar(select(Domain).where(Domain.domain == domain_name, Domain.deleted_at.is_(None)))
    if domain is None:
        domain = Domain(id=new_id("dom"), organization_id=None, domain=domain_name, type="customer_external",
                        status="active", inbound_status="connected", outbound_status="connected")
        session.add(domain)
        await session.flush()
    inbox = Inbox(id=new_id("ibx"), organization_id=organization_id, address=address, username=address.split("@")[0],
                  domain_id=domain.id, display_name=display_name, status="active", provider_mode="connected",
                  metadata_=metadata or {})
    session.add(inbox)
    await session.flush()
    conn = MailboxConnection(id=new_id("mbc"), organization_id=organization_id, inbox_id=inbox.id, provider=provider,
                             address=address, config_encrypted=encrypt_json(settings.app_secret_key, cfg),
                             last_uid=initial_uid,
                             sync_interval_seconds=settings.connector_sync_interval_seconds)
    session.add(conn)
    await session.flush()
    await emit(session, organization_id=organization_id, resource_type="inbox", resource_id=inbox.id, type="inbox.created",
               payload={"inbox_id": inbox.id, "connected": True, "provider": provider, "address": address})
    await enqueue(session, "connector_sync", {"connection_id": conn.id})
    return inbox, conn


async def _fresh_config(session: AsyncSession, settings: Settings, conn: MailboxConnection, http) -> dict:
    """Decrypt the connector config; extensions may refresh short-lived credentials in place (and they get persisted)."""
    cfg = decrypt_json(settings.app_secret_key, conn.config_encrypted)
    from agentbox.connectors.oauth import refresh_if_needed

    if conn.provider == "gmail" and cfg.get("auth") == "oauth" and cfg.get("transport") != "gmail_api":
        raise APIError(409, "oauth_reconnect_required", "Reconnect Gmail to replace the old full-mailbox permission with limited read/send permissions.")
    if await refresh_if_needed(http, settings, cfg):
        conn.config_encrypted = encrypt_json(settings.app_secret_key, cfg)
        await session.flush()
    for hook in registry().hooks(settings, "connector.refresh_config"):
        if await hook(session, settings, conn, cfg, http):
            conn.config_encrypted = encrypt_json(settings.app_secret_key, cfg)
            await session.flush()
    return cfg


async def smtp_config_for_inbox(session: AsyncSession, settings: Settings, inbox_id: str, http=None) -> dict | None:
    conn = await session.scalar(select(MailboxConnection).where(MailboxConnection.inbox_id == inbox_id,
                                                                MailboxConnection.status == "active"))
    if conn is None:
        return None
    cfg = await _fresh_config(session, settings, conn, http)
    if cfg.get("transport") == "gmail_api":
        return {"transport": "gmail_api", "config": cfg, "mail_from": conn.address}
    oauth = cfg.get("auth") == "oauth"
    return {"host": cfg["smtp_host"], "port": cfg["smtp_port"], "username": cfg["username"],
            "password": None if oauth else cfg.get("password"), "oauth_token": cfg.get("access_token") if oauth else None,
            "starttls": bool(cfg.get("smtp_starttls", not cfg.get("smtp_ssl", False))),
            "use_tls": bool(cfg.get("smtp_ssl", False)), "mail_from": conn.address}


async def list_connections(session: AsyncSession, organization_id: str) -> list[MailboxConnection]:
    return list(await session.scalars(select(MailboxConnection).where(MailboxConnection.organization_id == organization_id,
                                                                      MailboxConnection.status != "disconnected")
                                      .order_by(MailboxConnection.created_at)))


async def get_connection(session: AsyncSession, organization_id: str, connection_id: str) -> MailboxConnection:
    conn = await session.scalar(select(MailboxConnection).where(MailboxConnection.id == connection_id,
                                                                MailboxConnection.organization_id == organization_id,
                                                                MailboxConnection.status != "disconnected"))
    if conn is None:
        raise APIError(404, "not_found", "Connection not found.")
    return conn


async def disconnect(session: AsyncSession, conn: MailboxConnection, actor: str | None = None) -> None:
    """Stop syncing and soft-delete the inbox (messages already received stay readable through the API)."""
    conn.status = "disconnected"
    # Stop access locally even if the provider's grant has not yet been revoked.
    conn.config_encrypted = ""
    inbox = await session.get(Inbox, conn.inbox_id)
    if inbox is not None and inbox.deleted_at is None:
        inbox.status, inbox.deleted_at = "deleted", utcnow()
    await emit(session, organization_id=conn.organization_id, resource_type="inbox", resource_id=conn.inbox_id,
               type="inbox.deleted", payload={"inbox_id": conn.inbox_id, "disconnected": True, "actor": actor})


# ---------------------------------------------------------------- sync job

_client_factory = _open_client


def set_client_factory(factory) -> None:
    """Test hook: replace the IMAP client factory."""
    global _client_factory
    _client_factory = factory


async def sync_connection(session: AsyncSession, storage, settings: Settings, conn: MailboxConnection, http=None) -> int:
    try:
        cfg = await _fresh_config(session, settings, conn, http)
        gmail = cfg.get("transport") == "gmail_api"
        if gmail:
            fetched = [FetchedMail(uid=0, raw=raw) for raw in await GmailAPI(http, cfg).fetch_new()]
        else:
            client = await asyncio.to_thread(_client_factory, cfg)
            try:
                fetched = await asyncio.to_thread(client.fetch_new, conn.last_uid, 50)
            finally:
                await asyncio.to_thread(client.close)
    except Exception as e:  # noqa: BLE001
        conn.last_error = e.message if isinstance(e, APIError) else "Mailbox sync failed. Check the connection or reconnect."
        conn.last_sync_at = utcnow()
        log.warning("connector_sync_failed", connection=conn.id, error=conn.last_error)
        return 0
    count = 0
    for item in fetched:
        parsed = email_lib.message_from_bytes(item.raw)
        sender = normalize_email((parsed.get("From") or "").split("<")[-1].rstrip(">"))
        ingest_id = new_id("ing")
        key = f"org/{conn.organization_id}/raw/{ingest_id}.eml"
        await storage.put_bytes(key, item.raw, "message/rfc822")
        session.add(InboundIngest(id=ingest_id, organization_id=conn.organization_id, kind="message", inbox_id=conn.inbox_id,
                                  storage_key=key, mail_from=sender, rcpt_to=conn.address, size_bytes=len(item.raw),
                                  status="received"))
        await enqueue(session, "inbound_process", {"ingest_id": ingest_id})
        conn.last_uid = max(conn.last_uid, item.uid)
        count += 1
    if gmail:
        conn.config_encrypted = encrypt_json(settings.app_secret_key, cfg)
    conn.last_error = None
    conn.last_sync_at = utcnow()
    return count


async def connector_sync_job(ctx, session: AsyncSession) -> None:
    """Sync one connection when given an id, otherwise every active connection that is due."""
    settings = ctx.runtime.settings
    if ctx.payload.get("connection_id"):
        rows = [await session.get(MailboxConnection, ctx.payload["connection_id"])]
    else:
        rows = list(await session.scalars(select(MailboxConnection).where(MailboxConnection.status == "active")))
    for conn in rows:
        if conn is None or conn.status != "active":
            continue
        if not ctx.payload.get("connection_id") and conn.last_sync_at and \
                (utcnow() - conn.last_sync_at).total_seconds() < conn.sync_interval_seconds:
            continue
        n = await sync_connection(session, ctx.runtime.storage, settings, conn, http=ctx.runtime.http)
        if n:
            log.info("connector_synced", connection=conn.id, new_messages=n)
