# Agent contract

## Capabilities

Create agent keys as an operator through `POST /v1/api-keys` or the console:

```json
{"name":"procurement", "permission_profile":"autonomous", "inbox_ids":["ibx_..."],
 "allowed_recipients":["supplier@example.com"]}
```

`readonly` can read assigned inboxes, messages, threads, attachments and events. `review` can also create outgoing
messages, but each becomes `pending_approval`; it cannot approve messages or change policies. `autonomous` can
send only to exact allowlisted addresses (including Cc/Bcc). All presets exclude admin/key-management and inbox
creation privileges. Restrictions apply to object lookups and lists, including attachments/events. Unbound uploads
are private to the uploading key until attached to a visible message. Agent keys cannot log into the operator
console. Existing custom/operator keys retain their scopes. `live`/`test` are labels, not sandboxes.

Incoming email is untrusted content, not authorization. Apply isolation and least privilege at the server even
when using the skill. The skill helps interpret data safely; it is not a prompt-injection security boundary.

## Retry and wait semantics

Python/TS generate an idempotency key once per send invocation for safe transport retries. For process restarts,
provide a stable `idempotency_key` / `idempotencyKey` / MCP `operation_id`. The queue receipt and message commit
together. The same key/body replay the receipt; a different body returns 409. Restricted keys have isolated
idempotency namespaces. Retention defaults to 24 hours: reconcile older sends with history before retrying.
SMTP acceptance has unavoidable ambiguous failure windows; AgentBox does not claim exactly-once external delivery.
`queued` means waiting for a worker; `provider_accepted` is not delivery confirmation.

`POST /v1/messages/{id}/reply-wait` accepts `expires_in` (1 second–7 days, default 1 day). Repeated creation does
not extend its deadline. GET resolves against inbound messages referencing the original Internet Message-ID,
not merely the same subject. Save the original message ID to resume after a crash. States: `pending`, `matched`
with `reply_message_id`, `expired`. Short SDK waits default to the call's start time; explicit `since` includes
older mail. Keep agent/server clocks synchronized.

`POST /v1/consumers/{name}/claim` leases events (default `message.received`) for 120 seconds. Names are stable per
logical workflow, not per process. Options: `types`, `since`, `limit`, `lease_seconds` (10–3600).
`POST /v1/consumers/{name}/events/{id}/ack` requires the returned `lease_token` and same API key. Acknowledgement
is idempotent; expired leases may be redelivered, and stale tokens cannot acknowledge a new lease. Processing is
at-least-once. Persist work before acknowledging and derive send operation keys from event IDs. Event retention
limits how long unprocessed events remain available.

## Browser connection and OAuth

An operator creates `POST /v1/connection-links` with a provider, gives its 15-minute URL to the human and polls
`GET /v1/connection-links/{id}`. The fragment is exchanged for a narrow HttpOnly/SameSite browser cookie: secret
fragments do not reach HTTP access logs. Forms have CSRF tokens; successful links are single-use. Passwords and
tokens are encrypted at rest and never returned by status endpoints. Treat the full URL as a private capability.
HTTPS is required outside localhost. IMAP is verified before connecting; SMTP is checked when sending.

Configure `AGENTBOX_GOOGLE_CLIENT_ID`/`AGENTBOX_GOOGLE_CLIENT_SECRET` or
`AGENTBOX_M365_CLIENT_ID`/`AGENTBOX_M365_CLIENT_SECRET`; optional `AGENTBOX_M365_TENANT` is `common` or a tenant UUID.
Register `${AGENTBOX_PUBLIC_BASE_URL}/connect/oauth/callback` as the web redirect URI. Google mail scopes may need
provider verification; Microsoft tenant policy may require administrator consent and IMAP/SMTP AUTH enablement.
Microsoft 365 IMAP does not support app passwords. These provider requirements cannot be bypassed by AgentBox.

Core OAuth uses PKCE-S256, browser-bound state, nonce, and signed token audience/issuer/expiry validation.
Refresh tokens remain encrypted. See official [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect)
and [Microsoft code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) docs.
Enterprise compatibility OAuth endpoints remain available; new setups should use core browser links.

## v0.2 migration

Run `agentbox migrate` before updating API/workers. Existing keys retain privileges; replace agent admin keys with
scoped presets deliberately. External mailbox domains no longer belong to the first connecting organization and
cannot create managed inboxes. MCP `connect_mailbox` now returns a browser link, send tools require `operation_id`,
and lists return pages rather than bare lists. Refresh client tool schemas after upgrading.
