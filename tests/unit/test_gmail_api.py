import base64
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from agentbox.api.errors import APIError
from agentbox.connectors.gmail import GmailAPI, GmailProvider
from agentbox.connectors.oauth import GMAIL_SCOPES, gmail_scopes
from agentbox.mime.build import OutboundAttachment, OutboundMessage, build_mime
from agentbox.mime.parse import Address
from agentbox.providers.base import Envelope, PermanentError, TemporaryError


@pytest.mark.parametrize("scope", ["", "https://mail.google.com/", " ".join(GMAIL_SCOPES) + " https://mail.google.com/",
                                    "https://www.googleapis.com/auth/gmail.readonly",
                                    " ".join(GMAIL_SCOPES) + " https://www.googleapis.com/auth/gmail.modify"])
def test_missing_or_excessive_gmail_permissions_are_rejected(scope):
    with pytest.raises(APIError, match="Allow reading and sending"):
        gmail_scopes(scope)


async def test_incremental_pagination_deduplication_and_deleted_messages():
    cfg = {"access_token": "secret", "gmail_history_id": "100"}
    seen = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.method == "GET"
        seen.append(request)
        if request.url.path.endswith("/history"):
            assert request.url.params["startHistoryId"] == "100"
            assert request.url.params["historyTypes"] == "messageAdded"
            assert request.url.params["labelId"] == "INBOX"
            if request.url.params.get("pageToken") == "next":
                return httpx.Response(200, json={"historyId": "300"})
            messages = [{"message": {"id": mid, "threadId": "thread"}} for mid in ["a", "a", "gone"]]
            return httpx.Response(200, json={"historyId": "300", "nextPageToken": "next", "history": [{"messagesAdded": messages}]})
        if request.url.path.endswith("/gone"):
            return httpx.Response(404)
        assert request.url.path.endswith("/a") and request.url.params["format"] == "raw"
        return httpx.Response(200, json={"raw": base64.urlsafe_b64encode(b"From: customer@example.com\r\n\r\nhello").decode().rstrip("=")})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = GmailAPI(http, cfg)
        assert len(await api.fetch_new()) == 1
        assert cfg["gmail_history_id"] == "100" and cfg["gmail_page_token"] == "next"
        assert await api.fetch_new() == []
        assert cfg["gmail_history_id"] == "300" and "gmail_page_token" not in cfg
    assert len(seen) == 4


async def test_failed_page_does_not_advance_cursor_or_expose_provider_body():
    cfg = {"access_token": "secret", "gmail_history_id": "100"}

    def handler(request):
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json={"historyId": "300", "history": [{"messagesAdded": [
                {"message": {"id": "a", "labelIds": ["INBOX"]}}]}]})
        return httpx.Response(500, text="private-access-token-and-mail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(APIError) as exc:
            await GmailAPI(http, cfg).fetch_new()
        assert "private" not in str(exc.value)
    assert cfg == {"access_token": "secret", "gmail_history_id": "100"}


async def test_expired_history_is_reported_without_silently_skipping_mail():
    cfg = {"access_token": "secret", "gmail_history_id": "100"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        with pytest.raises(APIError) as exc:
            await GmailAPI(http, cfg).fetch_new()
        assert exc.value.code == "gmail_history_expired"
    assert cfg["gmail_history_id"] == "100"


async def test_optional_import_keeps_initial_history_boundary():
    cfg = {"access_token": "secret"}

    def handler(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "me@gmail.com", "historyId": "100"})
        if request.url.path.endswith("/messages"):
            assert request.url.params["labelIds"] == "INBOX"
            return httpx.Response(200, json={"messages": []})
        assert request.url.path.endswith("/history") and request.url.params["startHistoryId"] == "100"
        return httpx.Response(200, json={"historyId": "110"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = GmailAPI(http, cfg)
        await api.initialize("me@gmail.com", import_history=True)
        await api.fetch_new()
        assert cfg["gmail_history_id"] == "100" and not cfg["gmail_import_history"]
        await api.fetch_new()
        assert cfg["gmail_history_id"] == "110"


async def test_profile_must_match_verified_identity():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"emailAddress": "other@gmail.com", "historyId": "1"}))) as http:
        with pytest.raises(APIError, match="does not match"):
            await GmailAPI(http, {"access_token": "secret"}).initialize("me@gmail.com")


def draft():
    return OutboundMessage(message_id="<id@gmail.com>", from_=Address("me@gmail.com"),
        to=[Address("to@example.com")], cc=[Address("cc@example.com")], bcc=[Address("bcc@example.com", "Скрытая копия")],
        reply_to=[], subject="Reply", text="Hello", html=None, in_reply_to="<source@example.com>",
        references=["<source@example.com>"], headers=[], attachments=[OutboundAttachment("file.txt", "text/plain", b"attachment")])


async def test_send_uses_gmail_api_and_preserves_bcc_threading_and_attachments():
    message = draft()
    raw = build_mime(message)

    def handler(request):
        assert request.method == "POST" and request.url.path.endswith("/messages/send")
        assert request.headers["Authorization"] == "Bearer secret"
        submitted = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(json.loads(request.content)["raw"]))
        assert submitted["Bcc"].addresses[0].addr_spec == "bcc@example.com"
        assert submitted["Bcc"].addresses[0].display_name == "Скрытая копия"
        assert submitted["Cc"].addresses[0].addr_spec == "cc@example.com"
        assert submitted["In-Reply-To"] == "<source@example.com>"
        assert next(submitted.iter_attachments()).get_payload(decode=True) == b"attachment"
        return httpx.Response(200, json={"id": "gmail-sent-id"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GmailProvider(http, {"access_token": "secret"}).send(Envelope("me@gmail.com", [], "id"), message, raw)
        assert result.accepted and result.provider_message_id == "gmail-sent-id"
    assert b"Bcc:" not in raw


@pytest.mark.parametrize("status,expected", [(429, TemporaryError), (503, TemporaryError), (401, PermanentError), (400, PermanentError)])
async def test_send_errors_follow_worker_retry_contract(status, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="private-provider-details"))) as http:
        with pytest.raises(expected) as exc:
            await GmailProvider(http, {"access_token": "secret"}).send(None, draft(), build_mime(draft()))
        assert "private-provider-details" not in str(exc.value)
