import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from agentbox.jobs.handlers import default_handlers
from agentbox.jobs.worker import JobWorker

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


async def test_three_runnable_examples(client, live_api_url, org, runtime, smtp_edge, mailpit, tmp_path):
    stop = asyncio.Event()
    worker = asyncio.create_task(JobWorker(runtime, default_handlers(), concurrency=2).run(stop))

    async def setup(profile):
        inbox = (await client.post("/v1/inboxes", headers=org.headers, json={})).json()
        key = (await client.post("/v1/api-keys", headers=org.headers, json={
            "name": profile, "permission_profile": profile, "inbox_ids": [inbox["id"]],
            "allowed_recipients": ["supplier@example.com"],
        })).json()
        return inbox, {**os.environ, "AGENTBOX_API_URL": live_api_url, "AGENTBOX_API_KEY": key["api_key"],
                       "AGENTBOX_INBOX_ID": inbox["id"], "AGENTBOX_DEMO_SMTP_PORT": str(smtp_edge[1])}

    async def run(script, env, *args):
        result = await asyncio.to_thread(subprocess.run, [sys.executable, str(EXAMPLES / script), *args],
                                          env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    async def received(inbox):
        response = await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers,
                                    params={"direction": "inbound", "wait": 10})
        assert response.json()["data"], response.text

    try:
        inbox, env = await setup("autonomous")
        state = str(tmp_path / "quote.json")
        sent = await run("supplier_quote.py", env, "start", "--state", state, "--task-id", "example-rfq")
        repeated = await run("supplier_quote.py", env, "start", "--state", state, "--task-id", "example-rfq")
        assert sent == repeated and sent["status"] == "pending"
        assert len((await client.get(f"/v1/inboxes/{inbox['id']}/messages", headers=org.headers)).json()["data"]) == 1
        await run("demo_inject.py", env, "supplier_reply", "--reply-to-message", sent["message_id"])
        await received(inbox)
        resumed = await run("supplier_quote.py", env, "resume", "--state", state)
        assert resumed["status"] == "matched" and "1250.00" in resumed["untrusted_supplier_reply"]

        inbox, env = await setup("review")
        await run("demo_inject.py", env, "support_request")
        await received(inbox)
        draft = await run("support_review.py", env)
        assert draft["status"] == "pending_approval"
        idle = await run("support_review.py", env)
        assert idle["status"] == "idle"
        reply = (await client.get(f"/v1/messages/{draft['draft_message_id']}", headers=org.headers)).json()
        assert reply["status"] == "pending_approval" and reply["to"][0]["email"] == "customer@example.com"
        assert "API key" not in reply["text"] and env["AGENTBOX_API_KEY"] not in reply["text"]
        approved = await client.post(f"/v1/messages/{draft['draft_message_id']}/approve", headers=org.headers)
        assert approved.json()["status"] == "queued"
        assert (await mailpit.find("Re: Order 123 arrived damaged"))["To"][0]["Address"] == "customer@example.com"

        inbox, env = await setup("readonly")
        await run("demo_inject.py", env, "invoice")
        await received(inbox)
        extracted = await run("extract_attachments.py", env)
        rows = extracted["invoices"][0]["untrusted_rows"]
        assert [r["total"] for r in rows] == ["1250.00", "25.00"]
    finally:
        stop.set()
        await worker
