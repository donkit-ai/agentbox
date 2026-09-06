"""Read-only checks, plus an explicitly requested local-only roundtrip."""

import asyncio
from datetime import timedelta
from email.message import EmailMessage
from uuid import uuid4

from sqlalchemy import func, select, text

from agentbox.db.models import Job, MailboxConnection, Message, WorkerHeartbeat, utcnow
from agentbox.extensions import registry


async def diagnose(runtime, organization_id=None):
    checks = []

    def add(name, status, detail, action=""):
        checks.append({"name": name, "status": status, "detail": detail, "action": action})

    try:
        async with runtime.db.session() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), 5)
            add("database", "ok", "PostgreSQL is reachable.")
            try:
                workers = list(
                    await session.scalars(
                        select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at >= utcnow() - timedelta(seconds=150))
                    )
                )
                kinds = {kind for worker in workers for kind in worker.kinds}
                needed = {"outbound_send", "inbound_process", "connector_sync"}
                missing = sorted(needed - kinds)
                add(
                    "workers",
                    "warning" if missing else "ok",
                    f"{len(workers)} live worker(s); missing handlers: {', '.join(missing) or 'none'}.",
                    "Run agentbox worker (or docker compose up -d worker)." if missing else "",
                )
                jobs = select(Job).where(Job.status.in_(["pending", "dead"]))
                if organization_id:
                    jobs = jobs.where(Job.payload["organization_id"].astext == organization_id)
                delayed = 0
                dead = 0
                for job in await session.scalars(jobs):
                    dead += job.status == "dead"
                    delayed += job.status == "pending" and job.run_at < utcnow() - timedelta(minutes=5)
                add("job_queue", "warning" if delayed or dead else "ok",
                    f"{delayed} overdue job(s); {dead} dead job(s).",
                    "Inspect failed jobs and provider availability before retrying." if delayed or dead else "")
                for hook in registry().hooks(runtime.settings, "diagnostics.checks"):
                    checks.extend(await hook(session, runtime, organization_id))
                latest = select(func.max(Message.created_at)).where(Message.direction == "inbound")
                connections = select(MailboxConnection).where(MailboxConnection.status != "disconnected")
                if organization_id:
                    latest = latest.where(Message.organization_id == organization_id)
                    connections = connections.where(MailboxConnection.organization_id == organization_id)
                received = await session.scalar(latest)
                add(
                    "last_receive",
                    "ok" if received else "info",
                    received.isoformat() if received else "No inbound mail yet.",
                )
                for conn in await session.scalars(connections):
                    status = (
                        "warning" if conn.last_error or conn.status != "active" or conn.last_sync_at is None else "ok"
                    )
                    add(
                        f"mailbox:{conn.id}",
                        status,
                        f"{conn.address}: {conn.status}; last sync {conn.last_sync_at or 'never'}; "
                        f"login/sync error: {bool(conn.last_error)}.",
                        "Check provider authorization and connection status in Connectors."
                        if status != "ok"
                        else "",
                    )
            except Exception:  # noqa: BLE001 — never expose database URLs or credential-bearing exceptions
                add("schema", "error", "Schema checks failed.", "Run agentbox migrate before starting API/worker.")
    except Exception:  # noqa: BLE001
        add("database", "error", "Cannot reach PostgreSQL.", "Check AGENTBOX_DATABASE_URL and postgres service health.")
    try:
        async with runtime.storage._client() as storage:
            await asyncio.wait_for(storage.head_bucket(Bucket=runtime.storage.bucket), 5)
        add("storage", "ok", "Object storage bucket is reachable.")
    except Exception:  # noqa: BLE001
        add(
            "storage",
            "error",
            "Cannot access the object storage bucket.",
            "Check S3 endpoint/credentials and run agentbox migrate.",
        )
    settings = runtime.settings
    demo = settings.outbound_smtp_host in {"mailpit", "localhost", "127.0.0.1"} and settings.outbound_smtp_port == 1025
    add(
        "default_delivery",
        "warning" if demo else "info",
        "DEMO: the default relay captures mail in Mailpit; it does not deliver to external recipients."
        if demo
        else "A custom default SMTP relay is configured. Configuration alone does not prove delivery.",
        "For real mail connect an existing mailbox or configure an outbound provider." if demo else "",
    )
    if not settings.app_secret_key or settings.app_secret_key.startswith("CHANGE-ME"):
        add(
            "encryption_key",
            "error",
            "The default encryption secret is unsafe.",
            "Set a generated secret before connecting accounts. "
            "Do not replace an existing key without re-encrypting stored credentials.",
        )
    else:
        add("encryption_key", "ok", "A custom encryption secret is configured.")
    return {
        "ok": not any(c["status"] == "error" for c in checks),
        "checks": checks,
        "delivery_states": {
            "queued": "Waiting for worker",
            "provider_accepted": "Relay/provider accepted, not confirmed delivered",
            "delivered": "Delivery confirmation received",
        },
    }


async def demo_roundtrip(runtime, organization_id, inbox_id):
    """Send through Mailpit, inject a synthetic reply at the SMTP edge, observe API data/event creation.

    Never runs against a connected or real-domain mailbox. This proves local plumbing, not internet delivery.
    """
    from aiosmtplib import send

    from agentbox.extensions import registry
    from agentbox.providers.router import select_provider_account
    from agentbox.security.crypto import decrypt_json
    from agentbox.services.inboxes import get_active_inbox_for_send
    from agentbox.services.messages import OutboundDraft, create_outbound_message

    settings = runtime.settings
    if settings.outbound_smtp_host not in {"mailpit", "localhost", "127.0.0.1"} or settings.outbound_smtp_port != 1025:
        raise ValueError("Demo roundtrip only supports the local Mailpit relay on port 1025.")
    subject = "AgentBox doctor " + str(uuid4())
    async with runtime.db.session() as session:
        inbox = await get_active_inbox_for_send(session, organization_id, inbox_id)
        if inbox.provider_mode != "managed" or not inbox.address.endswith("@agentbox.local"):
            raise ValueError("Use a managed @agentbox.local demo inbox; real mailboxes are never used for this check.")
        if registry().hooks(settings, "outbound.provider"):
            raise ValueError("Disable custom outbound provider hooks before running a local demo probe.")
        account = await select_provider_account(
            session, organization_id, inbox_id=inbox_id, recipient_domain="example.com"
        )
        cfg = decrypt_json(settings.app_secret_key, account.config_encrypted)
        if (
            account.provider != "smtp_relay"
            or cfg.get("host") not in {"mailpit", "localhost", "127.0.0.1"}
            or cfg.get("port") != 1025
        ):
            raise ValueError("The effective route is not local Mailpit. No test message was created.")
        message = await create_outbound_message(
            session,
            runtime.storage,
            settings,
            organization_id=organization_id,
            inbox=inbox,
            draft=OutboundDraft(to=[{"email": "supplier@example.com"}], subject=subject, text="Local demo probe."),
        )
        await session.commit()
        address, mid, sent_id = inbox.address, message.internet_message_id, message.id
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        async with runtime.db.session() as session:
            sent = await session.get(Message, sent_id)
            if sent.status == "provider_accepted":
                break
            if sent.status in {"failed", "rejected", "pending_approval"}:
                raise ValueError(f"Demo send stopped at {sent.status}; inspect worker/policy diagnostics.")
        await asyncio.sleep(0.3)
    else:
        raise ValueError("Outbound job did not reach Mailpit in 30 seconds. Check the worker.")
    reply = EmailMessage()
    reply["From"], reply["To"], reply["Subject"], reply["In-Reply-To"] = (
        "supplier@example.com",
        address,
        "Re: " + subject,
        mid,
    )
    reply["Message-ID"] = f"<{uuid4()}@example.com>"
    reply.set_content("Synthetic supplier reply. Local demo only.")
    await send(reply, hostname=settings.doctor_smtp_host, port=settings.smtp_bind_port, start_tls=False, timeout=10)
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        async with runtime.db.session() as session:
            received = await session.scalar(
                select(Message).where(
                    Message.inbox_id == inbox_id, Message.in_reply_to == mid, Message.direction == "inbound"
                )
            )
            if received:
                return {
                    "ok": True,
                    "mode": "local-demo",
                    "sent_message_id": sent_id,
                    "reply_message_id": received.id,
                    "detail": "Worker sent to Mailpit; SMTP edge received synthetic reply; "
                              "inbound worker stored it. Not internet delivery.",
                }
        await asyncio.sleep(0.3)
    raise ValueError("Synthetic reply was not stored in 30 seconds. Check SMTP edge and inbound worker.")
