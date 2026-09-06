# Connecting email to Agentbox

Agentbox OSS runs independently under your control: no hosted account, billing setup or SaaS registration is
required. If you install it yourself, **you are the operator** who configures the mail provider. The public-service
case is different: the service operator supplies its OAuth app to end users.

The private browser link offers **Create email for an agent** (requires your domain and delivery setup) and
**Connect my Gmail** (your Google OAuth app). A personal installation can explicitly enable the advanced
IMAP/SMTP app-password form. Missing configuration is explained on the screen.

## Personal installation: app password

If your provider supports app passwords, run from the OSS checkout:

```sh
uv run agentbox init --build --mode mailbox --provider gmail --app-passwords --directory agentbox-mail --port 8100
```

For Yandex or a generic IMAP server, select `--provider yandex360` or `--provider imap`. Microsoft 365 requires OAuth.
Open the private URL and enter your address and app password in the expanded advanced form. The wizard issues an
inbox-scoped review key; the agent's outgoing drafts require your approval. This path needs neither your own domain
nor a Google Cloud application. It uses IMAP/SMTP access, not the restricted read/send permission set of the Gmail API.

Google app passwords require two-step verification and are not available for every account or organization policy;
see [Google's app-password requirements](https://support.google.com/accounts/answer/185833?hl=en).
Never enter the main Google account password into Agentbox.

## Personal installation: OAuth

Put the selected provider's client ID and secret in `mailbox.env` (mode 0600), outside version control:

```dotenv
AGENTBOX_GOOGLE_CLIENT_ID=your-client-id
AGENTBOX_GOOGLE_CLIENT_SECRET=your-client-secret
```

Run:

```sh
uv run agentbox init --build --mode mailbox --provider gmail --config mailbox.env --directory agentbox-mail --port 8100
```

Register `http://localhost:8100/connect/oauth/callback` for this local setup. The wizard copies only mail settings
into its generated private `.env`, so they survive restarts; database secrets and edition settings from another
installation are not imported. For Microsoft use `AGENTBOX_M365_CLIENT_ID`, `AGENTBOX_M365_CLIENT_SECRET` and
optionally `AGENTBOX_M365_TENANT`, with `--provider m365`.

A personal Google app may fall under Google's [verification exceptions](https://support.google.com/cloud/answer/13464323?hl=en).
Do not assume the public SaaS verification process is required for a personal installation. Google's external
**Testing** audience expires authorizations and offline refresh tokens after seven days for these Gmail scopes;
see [Google's audience documentation](https://support.google.com/cloud/answer/15549945?hl=en). Set the audience and
publishing state appropriate to your use, or reconnect when a test authorization expires.

## Owner setup: dedicated agent addresses

1. Set `AGENTBOX_MANAGED_DOMAIN` to the public domain used for agent addresses. Configure inbound delivery through
   the SMTP edge or an [inbound webhook provider](inbound-webhooks.md), including the required DNS records.
2. Configure an active outbound provider in Agentbox, or the default relay with `AGENTBOX_OUTBOUND_SMTP_HOST`,
   `AGENTBOX_OUTBOUND_SMTP_PORT`, credentials and `AGENTBOX_OUTBOUND_SMTP_STARTTLS`.
3. Verify real inbound and outbound delivery using a mailbox you control. Then set
   `AGENTBOX_MANAGED_DELIVERY_ENABLED=true` and restart the API and worker.

The flag records the owner's delivery check; it is not an automatic deliverability test. The browser and POST
endpoint also require an active domain with active inbound/outbound status and a configured outbound account.
Local/demo domains and Mailpit relays remain unavailable. Recipient-specific routing rules still determine the
route for each actual send; a successful setup does not guarantee delivery to every recipient.

## Owner setup: Google

1. In the owner's Google Cloud project, enable the Gmail API and configure the OAuth consent screen for Agentbox.
2. Create a Web OAuth client and register the exact callback:
   `${AGENTBOX_PUBLIC_BASE_URL}/connect/oauth/callback`. Use HTTPS outside localhost.
3. Set `AGENTBOX_GOOGLE_CLIENT_ID` and `AGENTBOX_GOOGLE_CLIENT_SECRET` on the API and worker. Store the secret in
   the deployment's secret manager or private environment file. Restart those services.
4. Request `openid`, `email`, `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.send`. For a public service, complete Google's applicable verification before launch; personal installations may qualify for the exceptions above.

The reading permission covers the entire mailbox, including existing email; choosing to sync only new incoming
messages is application behavior, not a restriction enforced by Google's grant. Agentbox requests no permission
to modify, trash or permanently delete Gmail messages. Drafts and approvals remain local to Agentbox.
`gmail.readonly` is a restricted scope. Public apps handling that data on servers require Google's applicable
verification and security assessment; see [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).

OAuth uses PKCE, browser-bound state, nonce and signed ID-token verification. Gmail API profile verification must
match the authorized identity. Missing read/send consent or a returned full-mailbox grant is rejected.
The legacy enterprise Gmail start URL now opens the same browser flow. Registering the old enterprise callback
alone no longer enables Gmail connections.

## Sync, sending and revocation

Gmail OAuth uses the Gmail API for both receiving and sending; it never falls back to IMAP/SMTP. The browser flow
captures a history cursor at connection and imports only subsequent incoming messages. Pages advance the cursor
in the same database transaction as inbound ingestion. Failed fetches keep the cursor for retry. If Gmail's
history cursor expires, the connection reports an error and requires reconnection; it does not silently discard
the gap. See [Google's history availability](https://developers.google.com/workspace/gmail/api/guides/sync).

Sending submits MIME through `messages.send`, including CC/BCC and attachments. Provider acceptance is recorded
as `provider_accepted`, not as confirmed delivery. Existing Agentbox approval and recipient policies still apply.

Disconnect in **Connectors** to stop synchronization and sending and erase locally stored connector credentials.
The user can also revoke the Google grant in [Google Account connections](https://myaccount.google.com/connections).
Disconnecting does not delete copies of messages already stored in Agentbox. Managed inboxes can be removed in
**Inboxes**. Existing retention/deletion controls govern stored mail.

## Self-hosting and migration

App passwords are disabled by default and always disabled in the cloud edition. An OSS operator can explicitly
set `AGENTBOX_ALLOW_APP_PASSWORD_CONNECTIONS=true` to show the advanced form and enable the password
connection API. The form opens automatically when neither OAuth nor dedicated addresses are configured.
These connections use IMAP/SMTP and do not have the limited Gmail API permission contract.
Microsoft 365 still requires OAuth. Missing Google configuration never enables a password fallback.

Existing Gmail OAuth connections created with the full-mailbox scope must be disconnected and reconnected.
Revoke their previous Agentbox grant in Google first. Refreshing an old token cannot narrow its permissions;
Agentbox refuses to use the old connector credentials. Existing password connections continue until disconnected.

Local integration tests use simulated Gmail responses, PostgreSQL, MinIO and Mailpit. They do not prove live Google
consent, OAuth verification or internet delivery. Test those separately with the owner's configured application.
