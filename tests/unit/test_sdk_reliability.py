import json

import httpx
import pytest

from agentbox_sdk import AgentBox


def test_transport_retry_reuses_generated_send_key(monkeypatch):
    monkeypatch.setattr("agentbox_sdk.client.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost response")
        return httpx.Response(202, json={"id": "msg_1"})

    with AgentBox("test", transport=httpx.MockTransport(handler)) as box:
        assert box.messages.send("ibx_1", to="a@example.com", text="hello")["id"] == "msg_1"
    assert len(calls) == 2
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]
    assert calls[0].extensions["timeout"]["read"] == 30


def test_non_idempotent_transport_failure_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response")

    with AgentBox("test", transport=httpx.MockTransport(handler)) as box, pytest.raises(httpx.ReadTimeout):
        box.request("POST", "/unprotected")
    assert len(calls) == 1


def test_wait_defaults_to_new_mail():
    def handler(request):
        assert request.url.params["since"]
        return httpx.Response(200, json={"data": [{"id": "new"}], "next_cursor": None})

    with AgentBox("test", transport=httpx.MockTransport(handler)) as box:
        assert box.messages.wait_for("inbox")["id"] == "new"


async def test_async_sdk_cancellation():
    import asyncio

    from agentbox_sdk import AsyncAgentBox

    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json={})

    async with AsyncAgentBox("test", transport=httpx.MockTransport(handler)) as box:
        task = asyncio.create_task(box.me())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_scoped_key_options_in_sync_and_async_sdk():
    from agentbox_sdk import AsyncAgentBox

    def handler(request):
        body = json.loads(request.content)
        assert body["permission_profile"] == "autonomous"
        assert body["inbox_ids"] == ["ibx_demo"]
        assert body["allowed_recipients"] == ["supplier@example.com"]
        assert "scopes" not in body
        return httpx.Response(201, json={"id": "key_test"})

    options = {"permission_profile": "autonomous", "inbox_ids": ["ibx_demo"],
               "allowed_recipients": ["supplier@example.com"]}
    with AgentBox("test", transport=httpx.MockTransport(handler)) as box:
        assert box.api_keys.create("agent", **options)["id"] == "key_test"
    async with AsyncAgentBox("test", transport=httpx.MockTransport(handler)) as box:
        assert (await box.api_keys.create("agent", **options))["id"] == "key_test"
