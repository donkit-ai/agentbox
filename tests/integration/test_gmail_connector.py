import base64
import json
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import httpx

from agentbox.connectors import oauth, service
from agentbox.db.models import MailboxConnection
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker
from agentbox.security.crypto import decrypt_json, encrypt_json


async def test_gmail_api_sync_refresh_send_and_disconnect(client, org, runtime, monkeypatch):
    source = EmailMessage()
    source["From"], source["To"] = "customer@example.com", "agent@gmail.com"
    source["Subject"], source["Message-ID"] = "New request", "<request@example.com>"
    source.set_content("Please reply.")
    source.add_attachment(b"original attachment", maintype="text", subtype="plain", filename="request.txt")
    sent = []
    refreshed = []

    def forbidden_imap(cfg):
        raise AssertionError("Gmail OAuth must never use IMAP")

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            assert b"grant_type=refresh_token" in request.content and b"refresh_token=private-refresh" in request.content
            refreshed.append(True)
            return httpx.Response(200, json={"access_token": "refreshed-access", "expires_in": 3600})
        assert request.url.host == "gmail.googleapis.com"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "agent@gmail.com", "historyId": "10"})
        assert request.headers["Authorization"] == "Bearer refreshed-access"
        if request.url.path.endswith("/history"):
            history = [] if request.url.params["startHistoryId"] == "20" else [{"id": "15", "messagesAdded": [
                {"message": {"id": "incoming-id", "threadId": "incoming-thread"}}]}]
            return httpx.Response(200, json={"historyId": "20", "history": history})
        if request.url.path.endswith("/messages/incoming-id"):
            return httpx.Response(200, json={"raw": base64.urlsafe_b64encode(source.as_bytes()).decode()})
        assert request.method == "POST" and request.url.path.endswith("/messages/send")
        sent.append(BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(json.loads(request.content)["raw"])))
        return httpx.Response(200, json={"id": "sent-id"})

    monkeypatch.setattr(service, "_client_factory", forbidden_imap)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(runtime, "http", http)
        async with runtime.db.session() as session:
            inbox, conn = await service.connect_mailbox(session, runtime.settings, organization_id=org.id,
                provider="gmail", address="agent@gmail.com", username=None, password=None, http=http,
                import_history=False, verify_credentials=True,
                oauth={"auth": "oauth", "oauth_provider": "gmail", "transport": "gmail_api", "scopes": sorted(oauth.GMAIL_SCOPES),
                       "access_token": "private-access", "refresh_token": "private-refresh", "expires_at": 0})
            await session.commit()
            inbox_id, connection_id = inbox.id, conn.id
        worker = JobWorker(runtime, default_handlers(), concurrency=1, kinds=["connector_sync", "inbound_process", "outbound_send"])
        await worker.drain()
        messages = (await client.get(f"/v1/inboxes/{inbox_id}/messages", headers=org.headers)).json()["data"]
        assert len(messages) == 1 and messages[0]["subject"] == "New request"
        assert refreshed == [True]
        await client.post(f"/v1/connections/{connection_id}/sync", headers=org.headers)
        await worker.drain()
        assert len((await client.get(f"/v1/inboxes/{inbox_id}/messages", headers=org.headers)).json()["data"]) == 1
        reply = await client.post(f"/v1/messages/{messages[0]['id']}/reply", headers=org.headers,
                                  json={"text": "Confirmed", "bcc": ["audit@example.com"]})
        assert reply.status_code in (200, 201, 202), reply.text
        await worker.drain()
        assert len(sent) == 1 and sent[0]["From"].addresses[0].addr_spec == "agent@gmail.com"
        assert sent[0]["In-Reply-To"] == "<request@example.com>" and sent[0]["Bcc"].addresses[0].addr_spec == "audit@example.com"
        result = (await client.get(f"/v1/messages/{reply.json()['id']}", headers=org.headers)).json()
        assert result["status"] == "provider_accepted" and result["provider"] == "gmail_api"
        async with runtime.db.session() as session:
            conn = await session.get(MailboxConnection, connection_id)
            cfg = decrypt_json(runtime.settings.app_secret_key, conn.config_encrypted)
            assert cfg["gmail_history_id"] == "20" and cfg["access_token"] == "refreshed-access"
            assert "imap_host" not in cfg and "smtp_host" not in cfg
            cfg["expires_at"] = 9999999999
            conn.config_encrypted = encrypt_json(runtime.settings.app_secret_key, cfg)
            await session.commit()
        assert (await client.delete(f"/v1/connections/{connection_id}", headers=org.headers)).status_code == 204
        async with runtime.db.session() as session:
            conn = await session.get(MailboxConnection, connection_id)
            assert conn.status == "disconnected" and not conn.config_encrypted
        assert (await client.post(f"/v1/inboxes/{inbox_id}/messages", headers=org.headers,
                                   json={"to": ["customer@example.com"], "text": "Should not send"})).status_code == 404
