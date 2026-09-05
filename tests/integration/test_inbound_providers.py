import asyncio
import base64
import hashlib
import hmac
import json
import time
from email.message import EmailMessage
from unittest.mock import AsyncMock
from urllib.parse import urlencode, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ByteStream
from sqlalchemy import func, select

from agentbox.db.models import Event, InboundEndpoint, InboundIngest, Inbox, Organization
from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker

SIGNING_KEY = "test-mailgun-signing-key"
PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_KEY = base64.b64encode(PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
)).decode()


async def endpoint(client, org, provider):
    cfg = {"signing_key": SIGNING_KEY} if provider == "mailgun" else (
        {"public_key": PUBLIC_KEY} if provider == "sendgrid" else {}
    )
    response = await client.post("/v1/inbound-endpoints", headers=org.headers, json={"provider": provider, **cfg})
    assert response.status_code == 201, response.text
    return response.json()


def vendor_reply(recipient, in_reply_to=None, message_id="<vendor-reply@supplier.ru>"):
    message = EmailMessage()
    message["From"] = "Sales <sales@supplier.ru>"
    message["To"] = recipient
    message["Subject"] = "Re: Quote"
    if message_id:
        message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content("Предложение во вложении.")
    message.add_alternative('<p>Предложение <img src="cid:logo"></p>', subtype="html")
    message.add_attachment(b"%PDF-1.4 offer", maintype="application", subtype="pdf", filename="offer.pdf")
    message.add_attachment(b"image-content", maintype="image", subtype="png", filename="logo.png",
                           disposition="inline", cid="<logo>")
    return message


def postmark_payload(recipient, in_reply_to=None):
    headers = [{"Name": "Message-ID", "Value": "<vendor-reply@supplier.ru>"}]
    if in_reply_to:
        headers += [{"Name": "In-Reply-To", "Value": in_reply_to}, {"Name": "References", "Value": in_reply_to}]
    return {
        "From": "sales@supplier.ru", "FromFull": {"Email": "sales@supplier.ru", "Name": "Sales"},
        "To": recipient, "OriginalRecipient": recipient, "MessageID": "postmark-delivery-1",
        "Subject": "Re: Quote", "TextBody": "Предложение во вложении.",
        "HtmlBody": '<p>Предложение <img src="cid:logo"></p>', "Headers": headers,
        "Attachments": [
            {"Name": "offer.pdf", "ContentType": "application/pdf",
             "Content": base64.b64encode(b"%PDF-1.4 offer").decode()},
            {"Name": "logo.png", "ContentType": "image/png", "ContentID": "logo",
             "Content": base64.b64encode(b"image-content").decode()},
        ],
    }


async def deliver(client, target, recipient, *, raw=None, payload=None, timestamp=None, valid=True, tamper=False):
    provider = target["provider"]
    path = urlsplit(target["url"]).path
    if provider == "postmark":
        credentials = target["credentials"]
        password = credentials["password"] if valid else "wrong"
        return await client.post(path, json=payload or postmark_payload(recipient),
                                 auth=(credentials["username"], password))
    ts = str(int(time.time()) if timestamp is None else timestamp)
    raw = vendor_reply(recipient).as_bytes() if raw is None else raw
    if provider == "mailgun":
        token = "provider-random-token"
        signature = hmac.new(SIGNING_KEY.encode(), (ts + token).encode(), hashlib.sha256).hexdigest()
        return await client.post(path, content=urlencode({
            "timestamp": ts, "token": token, "signature": signature if valid else "bad",
            "recipient": recipient, "sender": "bounce@supplier.ru", "body-mime": raw,
        }), headers={"Content-Type": "application/x-www-form-urlencoded"})
    recipients = recipient if isinstance(recipient, list) else [recipient]
    request = client.build_request("POST", path, data={
        "envelope": json.dumps({"from": "bounce@supplier.ru", "to": recipients}),
    }, files={"email": (None, raw, "message/rfc822")})
    body = await request.aread()
    signature = PRIVATE_KEY.sign(ts.encode() + body, ec.ECDSA(hashes.SHA256()))
    request.headers["X-Twilio-Email-Event-Webhook-Timestamp"] = ts
    request.headers["X-Twilio-Email-Event-Webhook-Signature"] = base64.b64encode(signature).decode() if valid else "bad"
    if tamper:
        request.stream = ByteStream(body + b"tampered")
        request.headers["Content-Length"] = str(len(body) + 8)
    return await client.send(request)


@pytest.mark.parametrize("provider", ["mailgun", "sendgrid", "postmark"])
async def test_receive_thread_attachments_event_and_reply(client, org, runtime, mailpit, provider):
    target = await endpoint(client, org, provider)
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "buyer"})).json()
    sent = (await client.post(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                              json={"to": ["sales@supplier.ru"], "subject": "Quote", "text": "Please quote"})).json()
    original = (await client.get(f"/v1/messages/{sent['id']}", headers=org.headers)).json()
    raw = vendor_reply(inbox["email"], original["internet_message_id"]).as_bytes()
    payload = postmark_payload(inbox["email"], original["internet_message_id"])
    response = await deliver(client, target, inbox["email"], raw=raw, payload=payload)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 1, "duplicates": 0, "ignored": 0}
    # Repeated deliveries before AND after worker processing must stay idempotent.
    response = await deliver(client, target, inbox["email"], raw=raw, payload=payload)
    assert response.json()["duplicates"] == 1
    await JobWorker(runtime, default_handlers(), concurrency=2).drain()
    response = await deliver(client, target, inbox["email"], raw=raw, payload=payload)
    assert response.json()["duplicates"] == 1
    messages = (await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                                 params={"direction": "inbound"})).json()["data"]
    assert len(messages) == 1
    received = messages[0]
    assert received["thread_id"] == sent["thread_id"]
    assert received["from"] == {"email": "sales@supplier.ru", "name": "Sales"}
    assert "во вложении" in received["text"] and "cid:logo" in received["html"]
    attachments = {a["filename"]: a for a in received["attachments"]}
    assert set(attachments) == {"offer.pdf", "logo.png"}
    assert attachments["logo.png"]["content_id"] == "logo"
    assert attachments["logo.png"]["disposition"] == "inline"
    async with runtime.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(InboundIngest)) == 1
        ingest = await session.scalar(select(InboundIngest))
        assert ingest.status == "stored"
        if provider != "postmark":
            assert await runtime.storage.get_bytes(ingest.storage_key) == raw
        events = (await session.scalars(select(Event).where(Event.type == "message.received"))).all()
        assert len(events) == 1 and events[0].payload["inbox_id"] == inbox["id"]
    reply = await client.post(f"/v1/messages/{received['id']}/reply", headers=org.headers,
                               json={"text": "Thanks, accepted."})
    assert reply.status_code == 202, reply.text
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    delivered = await mailpit.find("Re: Quote")
    assert "Thanks, accepted." in delivered["Text"]


@pytest.mark.parametrize("provider", ["mailgun", "sendgrid", "postmark"])
async def test_authentication_required(client, org, runtime, provider):
    target = await endpoint(client, org, provider)
    response = await deliver(client, target, "buyer@agentbox.local", valid=False)
    assert response.status_code == 401, response.text
    async with runtime.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(InboundIngest)) == 0


async def test_sendgrid_signature_binds_the_body(client, org):
    target = await endpoint(client, org, "sendgrid")
    response = await deliver(client, target, "buyer@agentbox.local", tamper=True)
    assert response.status_code == 401


@pytest.mark.parametrize("provider", ["mailgun", "sendgrid"])
async def test_raw_mime_setting_is_required(client, org, provider):
    target = await endpoint(client, org, provider)
    response = await deliver(client, target, "buyer@agentbox.local", raw=b"")
    assert response.status_code == 422
    assert "Raw MIME is required" in response.text


@pytest.mark.parametrize("provider", ["mailgun", "sendgrid"])
async def test_signature_timestamp_and_provider_retries(client, org, provider):
    target = await endpoint(client, org, provider)
    for timestamp in (int(time.time()) - 4 * 86400, int(time.time()) + 3600, "not-a-time"):
        response = await deliver(client, target, "buyer@agentbox.local", timestamp=timestamp)
        assert response.status_code == 401
    # A legitimate delayed provider retry is still acknowledged.
    response = await deliver(client, target, "buyer@agentbox.local", timestamp=int(time.time()) - 7 * 3600)
    assert response.status_code == 200


async def test_endpoint_management_hides_secrets_and_scopes_organizations(client, org, make_org, runtime):
    target = await endpoint(client, org, "postmark")
    another = await make_org("Another")
    listed = await client.get("/v1/inbound-endpoints", headers=org.headers)
    assert listed.status_code == 200
    assert "credentials" not in listed.text and target["credentials"]["password"] not in listed.text
    assert (await client.get("/v1/inbound-endpoints", headers=another.headers)).json()["data"] == []
    assert (await client.delete(f"/v1/inbound-endpoints/{target['id']}", headers=another.headers)).status_code == 404
    async with runtime.db.session() as session:
        stored = await session.get(InboundEndpoint, target["id"])
        assert target["credentials"]["password"] not in stored.config_encrypted
    assert (await client.delete(f"/v1/inbound-endpoints/{target['id']}", headers=org.headers)).status_code == 204
    assert (await deliver(client, target, "buyer@agentbox.local")).status_code == 404
    readonly = await make_org("Readonly", scopes=("providers:read",))
    assert (await client.post("/v1/inbound-endpoints", headers=readonly.headers,
                              json={"provider": "postmark"})).status_code == 403


@pytest.mark.parametrize("body", [
    {"provider": "mailgun"}, {"provider": "mailgun", "signing_key": ""},
    {"provider": "sendgrid"}, {"provider": "sendgrid", "public_key": "garbage"},
    {"provider": "postmark", "signing_key": "unused"}, {"provider": "other"},
])
async def test_invalid_endpoint_config(client, org, body):
    response = await client.post("/v1/inbound-endpoints", headers=org.headers, json=body)
    assert response.status_code == 422


async def test_only_envelope_recipients_in_own_org_are_delivered(client, org, make_org, runtime):
    target = await endpoint(client, org, "sendgrid")
    other = await make_org("Other")
    ours = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "ours"})).json()
    foreign = (await client.post("/v1/inboxes", headers=other.headers, json={"username": "theirs"})).json()
    # To is forged; the real recipient is ours (e.g. Bcc delivery).
    raw = vendor_reply(foreign["email"]).as_bytes()
    response = await deliver(client, target, [ours["email"].upper(), foreign["email"], "absent@agentbox.local"], raw=raw)
    assert response.json() == {"accepted": 1, "duplicates": 0, "ignored": 2}
    async with runtime.db.session() as session:
        ingests = (await session.scalars(select(InboundIngest))).all()
        assert len(ingests) == 1 and ingests[0].inbox_id == ours["id"]


@pytest.mark.parametrize("blocked", ["disabled", "receive_policy", "connected", "organization"])
async def test_receive_controls(client, org, runtime, blocked):
    target = await endpoint(client, org, "postmark")
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "blocked"})).json()
    if blocked == "receive_policy":
        response = await client.put(f"/v1/inboxes/{inbox['id']}/policy", headers=org.headers,
                                    json={"receive_enabled": False})
        assert response.status_code == 200, response.text
    else:
        async with runtime.db.session() as session:
            if blocked == "organization":
                row = await session.get(Organization, org.id)
                row.status = "suspended"
            else:
                row = await session.get(Inbox, inbox["id"])
                if blocked == "connected":
                    row.provider_mode = "connected"
                else:
                    row.status = "disabled"
            await session.commit()
    response = await deliver(client, target, inbox["email"])
    if blocked == "organization":
        assert response.status_code == 404
    else:
        assert response.json() == {"accepted": 0, "duplicates": 0, "ignored": 1}


async def test_concurrent_retries_without_message_id(client, org, runtime):
    target = await endpoint(client, org, "mailgun")
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "concurrent"})).json()
    raw = vendor_reply(inbox["email"], message_id=None).as_bytes()
    responses = await asyncio.gather(*(deliver(client, target, inbox["email"], raw=raw) for _ in range(5)))
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["accepted"] for r in responses) == 1
    assert sum(r.json()["duplicates"] for r in responses) == 4
    await JobWorker(runtime, default_handlers(), concurrency=4).drain()
    messages = (await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers)).json()["data"]
    assert len(messages) == 1


async def test_storage_failure_is_retryable(client, org, runtime, monkeypatch):
    target = await endpoint(client, org, "postmark")
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "retry"})).json()
    with monkeypatch.context() as patch:
        patch.setattr(runtime.storage, "put_bytes", AsyncMock(side_effect=OSError("storage offline")))
        assert (await deliver(client, target, inbox["email"])).status_code == 500
    async with runtime.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(InboundIngest)) == 0
    assert (await deliver(client, target, inbox["email"])).json()["accepted"] == 1


async def test_malformed_payload_and_size_limits(client, org, runtime, monkeypatch):
    target = await endpoint(client, org, "postmark")
    for payload in ({"OriginalRecipient": "x@y"}, {**postmark_payload("x@y"), "Attachments": [{"Name": "x", "Content": "!!"}]},
                    {**postmark_payload("x@y"), "Headers": [{"Name": "Subject", "Value": "Hi\r\nBcc: x@y"}]}):
        response = await deliver(client, target, "x@y", payload=payload)
        assert response.status_code == 422, response.text
    monkeypatch.setattr(runtime.settings, "max_inbound_bytes", 100)
    assert (await deliver(client, target, "x@y")).status_code == 413
    auth = (target["credentials"]["username"], target["credentials"]["password"])

    async def chunks():
        for _ in range(17):
            yield b"x" * 65536

    response = await client.post(urlsplit(target["url"]).path, auth=auth, content=chunks())
    assert response.status_code == 413


async def test_raw_form_field_larger_than_starlette_default(client, org, runtime):
    target = await endpoint(client, org, "sendgrid")
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "large"})).json()
    raw = b"From: sales@supplier.ru\r\nTo: " + inbox["email"].encode() + b"\r\n\r\n" + b"a" * (1024 * 1024 + 1)
    response = await deliver(client, target, inbox["email"], raw=raw)
    assert response.status_code == 200, response.text
    async with runtime.db.session() as session:
        ingest = await session.scalar(select(InboundIngest))
        assert await runtime.storage.get_bytes(ingest.storage_key) == raw


@pytest.mark.parametrize("provider", ["mailgun", "sendgrid"])
async def test_preserves_8bit_mime_in_legacy_charset(client, org, runtime, provider):
    target = await endpoint(client, org, provider)
    inbox = (await client.post("/v1/inboxes", headers=org.headers, json={"username": "legacy"})).json()
    raw = (b"From: sales@supplier.ru\r\nContent-Type: text/plain; charset=windows-1251\r\n"
           b"Content-Transfer-Encoding: 8bit\r\n\r\n" + "Привет, предложение".encode("cp1251"))
    response = await deliver(client, target, inbox["email"], raw=raw)
    assert response.status_code == 200, response.text
    async with runtime.db.session() as session:
        ingest = await session.scalar(select(InboundIngest))
        assert await runtime.storage.get_bytes(ingest.storage_key) == raw
    await JobWorker(runtime, default_handlers(), concurrency=1).drain()
    received = (await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers)).json()["data"]
    assert received[0]["text"] == "Привет, предложение"
