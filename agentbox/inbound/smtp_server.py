import asyncio
import ssl
import traceback
from pathlib import Path

import structlog
from aiosmtpd.smtp import SMTP, Envelope, Session
from sqlalchemy import select

from agentbox.db.models import InboundIngest, Inbox, Message, Organization, utcnow
from agentbox.domain.addresses import split_address
from agentbox.domain.ids import new_id
from agentbox.governance.policies import get_effective_policy
from agentbox.jobs.queue import enqueue
from agentbox.runtime import Runtime

log = structlog.get_logger("agentbox.inbound.smtp")


class InboundHandler:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def _resolve(self, address: str) -> dict | str | None:
        local, domain = split_address(address)
        if not local or not domain:
            return None
        async with self.runtime.db.session() as s:
            if local.startswith("bounce+"):
                message = await s.scalar(
                    select(Message).where(Message.id == f"msg_{local[7:].upper()}", Message.direction == "outbound")
                )
                if message is None:
                    return None
                inbox = await s.get(Inbox, message.inbox_id)
                org = await s.get(Organization, message.organization_id)
                if inbox is None or inbox.address.rpartition("@")[2] != domain:
                    return None
                if org is None or org.status != "active":
                    return "disabled"
                return {"kind": "bounce", "organization_id": message.organization_id, "inbox_id": message.inbox_id,
                        "bounce_message_id": message.id}
            inbox = await s.scalar(select(Inbox).where(Inbox.address == address, Inbox.deleted_at.is_(None)))
            if inbox is None:
                return None
            if inbox.provider_mode != "managed":
                return None
            org = await s.get(Organization, inbox.organization_id)
            if (org is None or org.status != "active" or inbox.status != "active"
                    or (inbox.expires_at and inbox.expires_at <= utcnow())):
                return "disabled"
            policy = await get_effective_policy(s, inbox.organization_id, inbox.id)
            if not policy.get("receive_enabled", True):
                return "disabled"
            return {"kind": "message", "organization_id": inbox.organization_id, "inbox_id": inbox.id,
                    "bounce_message_id": None}

    async def handle_RCPT(self, server: SMTP, session: Session, envelope: Envelope, address: str,
                          rcpt_options: list) -> str:
        addr = address.strip().lower()
        try:
            target = await self._resolve(addr)
        except Exception:  # noqa: BLE001
            log.error("rcpt_lookup_failed", address=addr, exc=traceback.format_exc())
            return "451 4.3.0 Temporary lookup failure, try again later"
        if target is None:
            return "550 5.1.1 No such user here"
        if target == "disabled":
            return "550 5.2.1 Mailbox disabled"
        envelope.rcpt_tos.append(addr)
        return "250 OK"

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        raw: bytes = envelope.original_content or b""
        if len(raw) > self.runtime.settings.max_inbound_bytes:
            return "552 5.3.4 Message too large"
        mail_from = (envelope.mail_from or "").strip().lower()
        last_id = None
        try:
            for rcpt in envelope.rcpt_tos:
                target = await self._resolve(rcpt)
                if not isinstance(target, dict):
                    continue
                ingest_id = new_id("ing")
                key = f"org/{target['organization_id']}/raw/{ingest_id}.eml"
                await self.runtime.storage.put_bytes(key, raw, "message/rfc822")
                async with self.runtime.db.session() as s:
                    s.add(InboundIngest(id=ingest_id, organization_id=target["organization_id"], kind=target["kind"],
                                        inbox_id=target["inbox_id"], bounce_message_id=target["bounce_message_id"],
                                        storage_key=key, mail_from=mail_from, rcpt_to=rcpt, size_bytes=len(raw),
                                        status="received"))
                    await enqueue(s, "inbound_process", {"ingest_id": ingest_id})
                    await s.commit()
                last_id = ingest_id
        except Exception:  # noqa: BLE001
            log.error("inbound_persist_failed", exc=traceback.format_exc())
            return "451 4.3.0 Temporary failure, try again later"
        if last_id is None:
            return "550 5.1.1 No valid recipients"
        log.info("inbound_accepted", ingest_id=last_id, mail_from=mail_from, size=len(raw))
        return f"250 2.0.0 Queued as {last_id}"


class TLSCertificates:
    """New connections see renewed certificates; existing TLS sessions keep their context."""
    def __init__(self, settings):
        self.settings, self.context, self.version = settings, None, None

    def get(self):
        s = self.settings
        if not s.smtp_tls_cert and not s.smtp_tls_key:
            return None
        if not s.smtp_tls_cert or not s.smtp_tls_key:
            raise ValueError("Both SMTP TLS certificate and key are required")
        try:
            cert = Path(s.smtp_tls_cert)
            key = Path(s.smtp_tls_key)
            # Resolve an atomic certificate-generation symlink once for both files.
            if cert.parent == key.parent:
                directory = cert.parent.resolve()
                cert, key = directory / cert.name, directory / key.name
            version = (str(cert), cert.stat().st_mtime_ns, str(key), key.stat().st_mtime_ns)
            if version != self.version:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(cert, key)
                self.context, self.version = context, version
                log.info("smtp_tls_certificate_loaded")
        except (OSError, ssl.SSLError):
            if self.context is None:
                raise
            log.warning("smtp_tls_reload_failed_keeping_previous_certificate")
        return self.context


async def start_smtp_server(runtime: Runtime, host: str | None = None, port: int | None = None) -> asyncio.Server:
    handler = InboundHandler(runtime)
    loop = asyncio.get_running_loop()
    certificates = TLSCertificates(runtime.settings)
    certificates.get()  # Fail clearly at startup when explicitly configured files are unavailable.
    settings = runtime.settings

    def factory() -> SMTP:
        return SMTP(handler, hostname=settings.smtp_hostname, data_size_limit=settings.max_inbound_bytes,
                    tls_context=certificates.get(), enable_SMTPUTF8=True, loop=loop)

    server = await loop.create_server(factory, host or settings.smtp_bind_host, port or settings.smtp_bind_port)
    log.info("smtp_edge_listening", host=host or settings.smtp_bind_host, port=port or settings.smtp_bind_port)
    return server
