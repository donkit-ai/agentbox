import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentbox.webhooks.signing import signature_header
from agentbox_sdk import AgentBox, AgentBoxError, AsyncAgentBox, verify_webhook_signature
from agentbox_sdk.mcp_server import build_server

ROOT = Path(__file__).resolve().parents[2]


async def test_python_sdk_end_to_end(live_api_url, org, runtime):
    mail = AgentBox(org.api_key, base_url=live_api_url)
    assert mail.me()["organization_id"] == org.id
    inbox = mail.inboxes.create("py-agent", display_name="Py", metadata={"lang": "py"})
    assert inbox["email"] == "py-agent@agentbox.local"
    assert [i["id"] for i in mail.inboxes.list(metadata={"lang": "py"})["data"]] == [inbox["id"]]

    sent = mail.messages.send(inbox["id"], to=["vendor@example.com"], subject="SDK hello", text="hi",
                              idempotency_key="py-idem-1")
    again = mail.messages.send(inbox["id"], to=["vendor@example.com"], subject="SDK hello", text="hi",
                               idempotency_key="py-idem-1")
    assert sent["id"] == again["id"]
    assert mail.messages.get(sent["id"], include_headers=True)["subject"] == "SDK hello"
    assert len(mail.threads.get(sent["thread_id"])["messages"]) == 1

    att = mail.attachments.upload("note.txt", b"hello attachment", "text/plain")
    rep = mail.messages.reply(sent["id"], text="follow-up", attachment_ids=[att])
    assert rep["thread_id"] == sent["thread_id"]
    assert mail.attachments.download(att) == b"hello attachment"
    assert mail.attachments.list(rep["id"])["data"][0]["filename"] == "note.txt"

    hook = mail.webhooks.create("https://example.com/h", event_types=["message.received"])
    header = signature_header(hook["secret"], b'{"id":"evt"}', 1_700_000_000)
    assert verify_webhook_signature(hook["secret"], header, b'{"id":"evt"}', now=1_700_000_010)
    assert not verify_webhook_signature("x", header, b'{"id":"evt"}', now=1_700_000_010)
    mail.webhooks.delete(hook["id"])

    assert mail.messages.wait_for(inbox["id"], timeout=1.5) is None
    assert mail.policy.set({"limits": {"emails_per_day": 10}})["effective"]["limits"]["emails_per_day"] == 10
    assert mail.suppressions.create("bad@example.com")["email"] == "bad@example.com"
    assert mail.analytics.delivery()["group_by"] == "provider"
    assert mail.events.list(type="inbox.created")["data"][0]["data"]["inbox_id"] == inbox["id"]
    with pytest.raises(AgentBoxError) as exc:
        mail.inboxes.get("ibx_nope")
    assert exc.value.code == "not_found" and exc.value.request_id.startswith("req_")
    mail.close()


async def test_mcp_tools(live_api_url, org):
    server = build_server(AgentBox(org.api_key, base_url=live_api_url))
    names = {t.name for t in await server.list_tools()}
    assert {"create_inbox", "send_email", "reply_email", "forward_email", "list_threads", "get_thread",
            "list_messages", "read_email", "list_attachments", "download_attachment", "wait_for_email",
            "upload_attachment", "list_pending_approvals"} <= names
    res = await server.call_tool("create_inbox", {"username": "mcp-agent"})
    inbox = _payload(res)
    assert inbox["email"] == "mcp-agent@agentbox.local"
    sent = _payload(await server.call_tool("send_email", {"inbox_id": inbox["id"], "to": ["v@example.com"],
                                                          "subject": "MCP", "text": "hello", "operation_id": "mcp-send-1"}))
    assert sent["status"] == "queued"
    thread = _payload(await server.call_tool("get_thread", {"thread_id": sent["thread_id"]}))
    assert thread["messages"][0]["preview"] == "hello"
    msgs = _payload(await server.call_tool("list_messages", {"inbox_id": inbox["id"], "direction": "outbound"}))
    assert [m["id"] for m in msgs["data"]] == [sent["id"]]
    err = _payload(await server.call_tool("read_email", {"message_id": "msg_nope"}))
    assert err["error"]["code"] == "not_found"
    waited = _payload(await server.call_tool("wait_for_email", {"inbox_id": inbox["id"], "timeout_seconds": 1}))
    assert waited is None


async def test_async_sdk_end_to_end(live_api_url, org):
    async with AsyncAgentBox(org.api_key, base_url=live_api_url) as mail:
        inbox = await mail.inboxes.create()
        await mail.inboxes.create()
        assert len([i async for i in mail.inboxes.iter(limit=1)]) == 2
        sent = await mail.messages.send(inbox["id"], to="vendor@example.com", text="async", idempotency_key="async-1")
        assert (await mail.messages.get(sent["id"]))["text"] == "async"
        wait = await mail.messages.start_reply_wait(sent["id"])
        assert wait["status"] == "pending"
        assert await mail.messages.reply_wait(sent["id"]) == wait
        aid = await mail.attachments.upload("async.txt", b"async", "text/plain")
        assert await mail.attachments.download(aid) == b"async"
        assert (await mail.connections.presets())[0]["key"]


async def test_remote_mcp_auth_and_tenant_isolation(live_api_url, org, make_org, monkeypatch):
    import httpx

    from agentbox_sdk.mcp_http import create_http_app

    monkeypatch.setenv("AGENTBOX_API_URL", live_api_url)
    # A configured admin key must NEVER be used as a fallback by remote requests.
    monkeypatch.setenv("AGENTBOX_API_KEY", org.api_key)
    other = await make_org()
    with AgentBox(org.api_key, base_url=live_api_url) as mail:
        inbox = mail.inboxes.create()
    app = create_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001",
        headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
    ) as client:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_inboxes", "arguments": {}}}
        assert (await client.post("/mcp", json=body)).status_code == 401
        assert (await client.post("/mcp", json=body, headers={"Authorization": "Bearer invalid"})).status_code == 401
        r = await client.post("/mcp", json=body, headers=org.headers)
        assert r.status_code == 200, r.text
        assert inbox["id"] in r.text, r.text
        r = await client.post("/mcp", json=body, headers=other.headers)
        assert r.status_code == 200 and inbox["id"] not in r.text, r.text
        assert (await client.post("/mcp", json=body, headers={**org.headers, "Origin": "https://evil.example"})).status_code == 403
        tools = await client.post("/mcp", headers=org.headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert '"name":"upload_file"' not in tools.text


def _payload(result):
    """Normalize FastMCP/MCPServer call_tool results across mcp versions into the tool's JSON value."""
    import json

    if isinstance(result, tuple):  # mcp 1.x: (content, structured)
        content, structured = result
        if structured is not None:
            return structured.get("result", structured) if isinstance(structured, dict) else structured
        result = content
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    blocks = getattr(result, "content", result)
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
async def test_typescript_sdk_smoke(live_api_url, org):
    ts = ROOT / "sdk" / "typescript"
    if not (ts / "dist" / "index.js").exists():
        subprocess.run(["npm", "run", "build"], cwd=ts, check=True, capture_output=True)
    env = {**os.environ, "AGENTBOX_API_URL": live_api_url, "AGENTBOX_API_KEY": org.api_key}
    r = subprocess.run(["node", "test/smoke.mjs"], cwd=ts, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ts sdk smoke ok" in r.stdout
