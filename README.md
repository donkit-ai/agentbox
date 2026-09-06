# AgentBox — email infrastructure for AI agents

Give a software agent a real email address with one API call, then send, receive, reply and forward with threads,
attachments and signed webhooks. Governed by policies, approval gates and suppressions; delivered through pluggable
providers; operated from a console; driven from Python, TypeScript or MCP. Apache-2.0, self-hostable,
with configurable policies and no paid-plan quotas. By Mikhail Baklanov and contributors.

| Area | Highlights |
|---|---|
| Core | inboxes (incl. ephemeral `ttl`) or connected existing mailboxes (Gmail, Yandex 360, M365, any IMAP), send / receive / reply / forward, thread resolution, attachments, signed webhooks with retries, idempotency, long-poll `wait=`, Postgres-backed job queue, own SMTP edge or inbound Mailgun / SendGrid / Postmark webhooks, VERP bounce handling on the SMTP edge |
| Domains | custom domain onboarding, DNS records to publish, background verification (ownership, MX, SPF, DKIM, DMARC) |
| Governance | org + inbox policies (allow/block lists, rate limits, loop protection, attachment rules), approval gates, suppressions, per-key API rate limit, audit events with NDJSON export |
| Delivery | provider accounts (SMTP relay, SendGrid, Unisender Go), routing rules, provider event webhooks, delivery analytics |
| SDKs | Python (`sdk/python`), TypeScript (`sdk/typescript`), MCP server (`agentbox-mcp`) |
| Console | `/dashboard`: overview, thread viewer, domains, webhooks, keys, usage, policies, audit, API console |

A hosted edition with sign-up, teams, billing and SSO/SCIM is built on top of this core through
`agentbox/extensions.py`; the core never depends on it.

## Quickstart for agent builders

From this checkout, with Docker/Compose and uv installed:

    uv run agentbox init --build --mode demo

This starts an isolated stack, generates secrets and an inbox-scoped key, and verifies a local email roundtrip.
Mailpit captures demo mail; it does not deliver to the internet. With Google OAuth configured by the operator,
use `--mode mailbox --provider gmail --config mailbox.env` to connect real mail through Google's consent page. The same browser flow
also offers a dedicated agent address when external delivery is enabled. See [owner setup](docs/mail-connection.md)
and [the full guide](docs/agents.md) for client-specific configs and examples.

Any MCP-capable agent (OpenClaw, Hermes Agent, Claude Desktop, Cursor, Codex) gets email tools with one config entry,
no install step:

    "agentbox": { "command": "uvx",
                  "args": ["--from", "/ABSOLUTE/PATH/agentbox/sdk/python[mcp]", "agentbox-mcp"],
                  "env": { "AGENTBOX_API_URL": "http://localhost:8000", "AGENTBOX_API_KEY": "ab_live_..." } }

This checkout uses the local SDK path until v0.2.0 is released; the guide includes pinned public-install templates.
An operator can connect the user's own Gmail / Yandex / Microsoft 365 / IMAP mailbox with a
browser connection link: Gmail uses the Gmail API with read/send permissions; Microsoft 365 requires OAuth.
App passwords are an explicitly enabled OSS advanced option. `docs/agents.md` has
the details and `skills/agentbox/SKILL.md` is a drop-in skill for OpenClaw and Hermes.

## Connect your own mailbox

For a personal self-hosted installation, you configure your own connection. You do not need a hosted AgentBox
account or a paid plan. Choose either Google/Microsoft OAuth or an explicitly enabled app-password connection.

Google OAuth: put `AGENTBOX_GOOGLE_CLIENT_ID` and `AGENTBOX_GOOGLE_CLIENT_SECRET` in a private `mailbox.env`,
register `http://localhost:8100/connect/oauth/callback`, then run:

    uv run agentbox init --build --mode mailbox --provider gmail --config mailbox.env --directory agentbox-mail --port 8100

If your mail provider supports app passwords, a personal installation can use:

    uv run agentbox init --build --mode mailbox --provider gmail --app-passwords --directory agentbox-mail --port 8100

Enter the address and **app password in the browser**. The flag enables that form; it never accepts a password
on the command line. Use `--provider yandex360` or `--provider imap` for other IMAP/SMTP providers. Microsoft 365
requires its own OAuth configuration. See [mail connection setup](docs/mail-connection.md) for permissions and
provider requirements. Mail settings are saved in the generated private `.env` and survive a restart.

The demo needs no Google application or real mail account. Real mailbox mode checks its configuration before
starting Docker, so a missing OAuth setup cannot leave the wizard waiting at an unusable screen.

## Run services manually (advanced)

Keep the admin key below for the human operator; issue an inbox-scoped agent preset from the console.

    cp .env.example .env
    make up                      # postgres (:5434), minio (:9010), mailpit (:1025 / :8025)
    uv sync --extra dev
    uv run agentbox migrate
    uv run agentbox org create "My Org"   # prints an admin API key (shown once)
    uv run agentbox api          # http://localhost:8000  (docs at /docs, console at /dashboard)
    uv run agentbox worker       # in another shell
    uv run agentbox smtp         # in another shell, inbound SMTP on :2525

Everything in Docker:

    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox migrate
    docker compose -f deploy/docker/docker-compose.yml --profile app run --rm api agentbox org create "My Org"
    docker compose -f deploy/docker/docker-compose.yml --profile app up --build -d
    # http://localhost:8000/dashboard  → log in with the API key

Production self-hosting (DNS records, TLS, relay): `docs/self-host.md`.
To receive on your own domain over HTTPS without port 25, follow [Inbound provider webhooks](docs/inbound-webhooks.md).

## Hello world

Load the wizard's `agent.env` into the process environment without printing it. The inbox already exists:

    curl -s "$AGENTBOX_API_URL/v1/inboxes/$AGENTBOX_INBOX_ID/messages" -H "Authorization: Bearer $AGENTBOX_API_KEY" \
      -H 'Content-Type: application/json' -H 'Idempotency-Key: first-hello' \
      -d '{"to":["supplier@example.com"],"subject":"Hello","text":"Local demo"}'

To wait for the reply across process restarts, start `/v1/messages/MESSAGE_ID/reply-wait` and read it later.
See the [supplier example](examples/README.md) for a complete send, retry, fixture reply and resume workflow.

Python:

    pip install ./sdk/python        # agentbox-sdk
    import os
    from agentbox_sdk import AgentBox
    with AgentBox(os.environ["AGENTBOX_API_KEY"], base_url=os.environ["AGENTBOX_API_URL"]) as mail:
        sent = mail.messages.send(os.environ["AGENTBOX_INBOX_ID"], to="supplier@example.com",
            subject="Quote", text="Please quote 100 units.", idempotency_key="quote-order-123")
        waiting = mail.messages.start_reply_wait(sent["id"])

TypeScript: `cd sdk/typescript && npm install && npm run build`, then `new AgentBox({ apiKey, baseUrl })`.
MCP: `pip install './sdk/python[mcp]'` and run `agentbox-mcp` with `AGENTBOX_API_KEY` / `AGENTBOX_API_URL`.

## API surface

| Area | Endpoints |
|---|---|
| Inboxes | `POST/GET /v1/inboxes`, `GET /v1/inboxes/{id}`, `POST .../disable`, `POST .../enable`, `DELETE`, `GET/PUT/DELETE .../policy` |
| Connections | `GET /v1/connections/presets`, `POST/GET /v1/connections`, `GET /v1/connections/{id}`, `POST .../sync`, `POST .../pause`, `POST .../resume`, `DELETE` (existing IMAP/SMTP mailbox as an inbox) |
| Browser connection | `POST /v1/connection-links`, `GET /v1/connection-links/{id}`; credentials/OAuth stay in the browser |
| Agent workflows | `POST/GET /v1/messages/{id}/reply-wait`, `POST /v1/consumers/{name}/claim`, `POST .../events/{id}/ack` |
| Inbound providers | `POST/GET /v1/inbound-endpoints`, `DELETE /v1/inbound-endpoints/{id}`, authenticated provider callbacks at `POST /v1/inbound/{provider}/{id}` (Mailgun: append `/mime`) |
| Messages | `POST /v1/inboxes/{id}/messages`, `GET /v1/inboxes/{id}/messages` (filters + `wait=`), `GET /v1/messages/{id}`, `POST .../reply`, `POST .../forward`, `POST .../approve`, `POST .../reject`, `GET /v1/approvals` |
| Threads | `GET /v1/inboxes/{id}/threads`, `GET /v1/threads/{id}` |
| Attachments | `POST /v1/attachments/uploads`, `GET /v1/attachments/{id}`, `GET .../download`, `GET /v1/messages/{id}/attachments` |
| Webhooks | `POST/GET/PATCH/DELETE /v1/webhooks`, `GET /v1/webhooks/{id}/deliveries`, `POST .../retry` |
| Domains | `POST/GET /v1/domains`, `GET /v1/domains/{id}`, `POST .../verify`, `DELETE` |
| Governance | `GET/PUT/DELETE /v1/policy`, `GET/POST/DELETE /v1/suppressions` |
| Delivery | `/v1/provider-accounts`, `/v1/routing-rules`, `POST /v1/providers/{provider}/events/{token}`, `GET /v1/analytics/delivery` |
| Account | `GET /v1/me`, `/v1/api-keys`, `GET /v1/usage`, `GET /v1/events`, `GET /v1/events/export` (NDJSON) |

Auth: `Authorization: Bearer ab_live_...` with scopes, `Idempotency-Key` on mutations, error envelope
`{"error":{"code","message","request_id","details"}}`, `AgentBox-Request-Id` on every response. Webhook signature
`AgentBox-Signature: t=<unix>,v1=<hmac-sha256>`.

## Extending

`agentbox/extensions.py` documents the entry point (`agentbox.extensions`) and hooks an edition or plugin can use:
routers, templates, job kinds, CLI commands, migrations, models, a `Settings` subclass, console login/shell hooks,
pre-create/pre-send checks and an outbound provider override.

## Tests

    make test              # unit
    make test-integration  # needs `make up` (API, jobs, SMTP edge, SDKs, MCP, console)
    make e2e               # acceptance walk-through

## Docs

`docs/self-host.md` (production), `docs/superpowers/specs/` (design notes), `CONTRIBUTING.md`.

## License

Apache-2.0. Copyright 2026 Mikhail Baklanov and AgentBox contributors.
