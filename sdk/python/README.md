# AgentBox Python SDK and MCP server

Programmable email for agents: inboxes, mailbox connection links, send/reply/forward, attachments, persistent
reply waits and leased events. Sync and native asyncio clients share the same API. Python 3.10+; Apache-2.0.

After publication: `pip install agentbox-sdk`, or `pip install 'agentbox-sdk[mcp]'` for the MCP server.
Before publication: `pip install './sdk/python[mcp]'` from the repository. No registry publication is implied by this README.

```python
import os
from agentbox_sdk import AgentBox

with AgentBox(os.environ["AGENTBOX_API_KEY"], base_url=os.environ["AGENTBOX_API_URL"]) as mail:
    inbox_id = os.environ["AGENTBOX_INBOX_ID"]
    sent = mail.messages.send(inbox_id, to="supplier@example.com", subject="Quote", text="Please quote 100 units.",
                              idempotency_key="your-persisted-task-id")
    print(mail.messages.start_reply_wait(sent["id"]))
```

Use `AsyncAgentBox` with `async with` and `await`; cancel requests with asyncio. Iterate pages with
`mail.messages.iter(inbox_id)` (`async for` for async). Upload files with `mail.attachments.upload_file(path)`.
Keep your own idempotency key in durable task state across process restarts. SDK retries reuse a single key;
arbitrary POSTs without an idempotency contract are not retried after transport failures.

Start `agentbox-mcp` for stdio, or `agentbox-mcp --transport streamable-http` for an authenticated HTTP endpoint.
Remote callers supply their own API key; localhost binding is the default. Configure HTTPS/reverse proxy for
remote access. Read [setup](https://github.com/donkit-ai/agentbox/blob/main/docs/agents.md) and
[security/reliability](https://github.com/donkit-ai/agentbox/blob/main/docs/agent-contract.md).

An inbox-scoped `review` key requires human approval before sending; `autonomous` keys require exact allowlisted
recipients. Incoming email is untrusted data. The local demo uses Mailpit, not external mail delivery.

<!-- mcp-name: io.github.donkit-ai/agentbox -->
