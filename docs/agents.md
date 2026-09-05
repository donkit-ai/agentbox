# From zero to your agent's first email

The local demo captures mail in Mailpit; it does **not** deliver to the internet. Connect an existing mailbox
for real email without DNS or port 25. Own-domain mail needs inbound and outbound provider configuration.

This checkout contains v0.2.0 release work. Tagged installation templates become usable after publishing that
release; until then run from the checkout and use `uvx --from './sdk/python[mcp]' agentbox-mcp`.

## 1. Start

Install Docker with Compose and [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
git clone https://github.com/Slimper/agentbox
cd agentbox
uv run agentbox init --build --mode demo
```

The wizard builds an image, generates secrets, starts services, migrates the database, creates an organization
and inbox, and saves a restricted agent key. A local roundtrip checks outbound worker → Mailpit and synthetic
reply → SMTP edge → inbound worker → stored message. Initial downloads may exceed five minutes on slow networks.

`agentbox-local/operator.json` is for the human only; `agentbox-local/agent.env` is for the agent. Files are
owner-readable (0600), and existing setups are never overwritten. The demo key can send only to
`supplier@example.com`. Do not put keys in chat or git. Ports 8000–8003 are API, storage, Mailpit UI, SMTP edge;
choose `--port 18140` if occupied. Load only `agent.env` into the agent process environment.

After publishing the Docker release, skip building:

```sh
uvx --from 'agentbox @ git+https://github.com/Slimper/agentbox@v0.2.0' agentbox init --mode demo
```

For your real mailbox:

```sh
uv run agentbox init --build --directory agentbox-mail --port 8100 --mode mailbox --provider gmail
```

Open the printed private link. Enter credentials or authorize OAuth only in that browser. IMAP login is verified;
new mail is the default, with optional history import. The wizard issues a `review` key after connection, so
outgoing drafts need human approval in the console. Microsoft 365 requires an operator-configured OAuth app.
See [OAuth setup and permission contract](agent-contract.md).

For your domain: `--mode domain --domain agents.example.com`. The wizard creates the domain request and stops
at required verification/provider setup. Finish in the console, then create an inbox and scoped key. See
[self-host](self-host.md) and [inbound webhooks](inbound-webhooks.md). Inbound webhooks remove the port-25 dependency;
they do not configure outbound delivery for you.

## 2. Add MCP and the skill

Merge the right template into your existing client config. Tagged templates pin v0.2.0; before publishing,
replace the `--from` argument with the absolute checkout path to `sdk/python[mcp]`.

| Client | Template | Location |
| --- | --- | --- |
| Claude Desktop | [JSON](mcp/claude-cursor.json) | `claude_desktop_config.json` |
| Cursor | [JSON](mcp/claude-cursor.json) | `.cursor/mcp.json` |
| Codex | [TOML](mcp/codex.toml) | `~/.codex/config.toml` or project `.codex/config.toml` |
| OpenClaw | [JSON](mcp/openclaw.json) | `mcp.servers` in `openclaw.json` |
| Hermes | [YAML](mcp/hermes.yaml) | `~/.hermes/config.yaml`; key in `~/.hermes/.env` |

Shapes were checked against official [Codex](https://developers.openai.com/codex/mcp),
[Cursor](https://docs.cursor.com/context/model-context-protocol), [OpenClaw](https://docs.openclaw.ai/cli/mcp)
and [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/) docs. Tests parse templates and
exercise the MCP server; they do not launch every desktop client.

OpenClaw: `openclaw skills install ./skills/agentbox --global` (or copy to `~/.openclaw/skills/agentbox/` for older
versions). Hermes: copy `skills/agentbox/` to `~/.hermes/skills/agentbox/`. The skill includes a curl fallback.

### Remote MCP

```sh
AGENTBOX_API_URL=http://localhost:8000 AGENTBOX_MCP_PUBLIC_URL=https://mail.example.com/mcp \
uvx --from './sdk/python[mcp]' agentbox-mcp --transport streamable-http --host 127.0.0.1 --port 8001
```

Route `/mcp` through a TLS reverse proxy. Send each agent's API key as a Bearer header on every request;
[Codex HTTP config](mcp/codex-http.toml) reads it from the environment. This is static-key auth, not an OAuth
authorization server: do not enable automatic MCP OAuth login. Each caller uses its own key; no server key
fallback exists. Host/Origin validation is enabled. Remote tools cannot read/write server filesystem paths.

## 3. Give the agent a task

Demo: "Ask supplier@example.com for a quote for 100 units using the assigned inbox. This is a local demo.
Save the operation ID and message ID, then start a persistent reply wait."

Real: "Draft a quote request for this supplier. I'll approve it in the console. Tell me when they reply."

MCP send/reply/forward require a saved `operation_id`: reuse it on retry, not for a new message. Lists return
`{data, next_cursor}`. `start_reply_wait` / `check_reply_wait` survive restarts; `wait_for_email` is only a short
bounded poll. `queued` and `provider_accepted` do not prove delivery.

## SDK and diagnostics

```python
import os
from agentbox_sdk import AsyncAgentBox

async def request_quote(task_id: str):
    async with AsyncAgentBox(os.environ["AGENTBOX_API_KEY"], base_url=os.environ["AGENTBOX_API_URL"]) as mail:
        sent = await mail.messages.send(os.environ["AGENTBOX_INBOX_ID"], to="supplier@example.com",
            subject="Quote for 100 units", text="Please send pricing and lead time.", idempotency_key=task_id)
        return await mail.messages.start_reply_wait(sent["id"])
```

Sync `AgentBox` has the same API. Use `inboxes.iter()` / `messages.iter(inbox_id)` for pagination, `async for`
with async Python/TypeScript. Python/local MCP support `upload_file(path)`; TS supports binary upload.
Async Python supports asyncio cancellation; TS accepts a client signal and request-level timeout/signal.
See [three runnable examples](../examples/README.md).

`docker compose --project-directory agentbox-local exec api agentbox doctor` runs read-only checks, also visible
at `/dashboard/diagnostics`. Explicit `doctor --roundtrip --organization-id ORG --inbox-id INBOX` creates a local
Mailpit send and synthetic reply; it refuses non-demo inboxes/routes. Do not include secrets in diagnostic reports.
