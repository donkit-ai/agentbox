# Receive email over HTTPS without port 25

Mailgun, SendGrid and Postmark can receive mail for your domain and POST it to AgentBox. The API stores the message
and queues the existing inbound worker, so threads, attachments, `message.received` events, MCP and SDK reads work
as they do for SMTP. All of this is part of the Apache-2.0 core.

## Prepare AgentBox

1. Set `AGENTBOX_API_BASE_URL` to the public HTTPS API URL (for example `https://mail.example.com`).
   Set `AGENTBOX_PUBLIC_BASE_URL` to the same URL for the console.
2. Set `AGENTBOX_MANAGED_DOMAIN` to the domain receiving mail (for example `agents.example.com`). Set
   `AGENTBOX_MX_HOSTNAMES` to the comma-separated MX hostnames provided by your inbound provider. This also makes
   AgentBox's custom-domain DNS instructions and verification use the provider's MX records.
3. Upgrade the database with `agentbox migrate`, and run the API and worker. The SMTP edge is optional.
   With the development Compose file, start only the required services:

       docker compose -f deploy/docker/docker-compose.yml --profile app run --build --rm api agentbox migrate
       docker compose -f deploy/docker/docker-compose.yml --profile app up --build -d api worker

   On a fresh installation, create an organization with `agentbox org create "My Org"` using `compose ... run --rm api`.
   If an SMTP container is already running and you no longer use it, stop it with `compose ... stop smtp`.
4. Publish the inbound provider's MX records. Create the inboxes that should receive mail. For additional custom
   domains, register them through `/v1/domains`, publish the `_agentbox` ownership TXT record and provider MX records,
   then verify them before creating inboxes. `AGENTBOX_MX_HOSTNAMES` is deployment-wide: use the same inbound provider
   for custom domains on one deployment.
5. Create an inbound endpoint below using an API key with `providers:write` (or `admin`). The returned URL is bound
   to that key's organization. Set `BASE=https://mail.example.com` and `KEY=ab_live_...` for the examples.

The provider delivers to existing active managed inboxes using its **SMTP envelope recipient**, including Bcc.
MIME `To` and `Cc` headers do not choose the destination inbox. Addresses in another organization, connected IMAP
mailboxes, expired/disabled inboxes, unknown addresses and inboxes with `receive_enabled=false` are ignored.

## Mailgun

Use the HTTP webhook signing key from your Mailgun account (not the API key):

    curl "$BASE/v1/inbound-endpoints" -H "Authorization: Bearer $KEY" \
      -H 'Content-Type: application/json' \
      -d '{"provider":"mailgun","name":"Mailgun inbound","signing_key":"YOUR_HTTP_WEBHOOK_SIGNING_KEY"}'

In Mailgun, configure a receiving domain and its regional MX records. Create a route matching your domain, with
`forward("RETURNED_URL")` and `stop()` actions. The returned URL ends in `/mime`; preserve that suffix. Mailgun then
includes the full email in `body-mime`, preserving the original MIME headers and attachments. Parsed-only payloads
are rejected with a configuration hint. AgentBox verifies Mailgun's `timestamp`, `token` and HMAC signature.

Mailgun documents the form fields, `/mime` behavior and retry responses in
[Receiving messages via HTTP](https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/receive-http).

## SendGrid

Configure an authenticated receiving domain and point its MX to `mx.sendgrid.net`. Create an Inbound Parse
security policy with `signature.enabled=true`, then use the returned `policy.signature.public_key`:

    curl "$BASE/v1/inbound-endpoints" -H "Authorization: Bearer $KEY" \
      -H 'Content-Type: application/json' \
      -d '{"provider":"sendgrid","name":"SendGrid inbound","public_key":"BASE64_DER_PUBLIC_KEY"}'

In Settings → Inbound Parse, set the destination to the returned URL and enable **POST the raw, full MIME message**
(`send_raw=true`). Associate the signing security policy with that Parse setting using the `security_policy` field.
Use the key from the **Inbound Parse** security policy, not an unrelated outbound Event Webhook key.
AgentBox verifies the ECDSA signature over the timestamp and exact HTTP body before parsing the form. It reads the
MIME from `email` and delivery addresses from `envelope.to`; parsed-only payloads are rejected with a configuration hint.

See SendGrid's [Parse setup](https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/setting-up-the-inbound-parse-webhook)
and [signing security policy setup](https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/securing-your-parse-webhooks).

## Postmark

    curl "$BASE/v1/inbound-endpoints" -H "Authorization: Bearer $KEY" \
      -H 'Content-Type: application/json' -d '{"provider":"postmark","name":"Postmark inbound"}'

The create response includes `credentials.username` and `credentials.password`, shown **once**. Save them and set
the Inbound Message Stream's webhook URL to:

    https://USERNAME:PASSWORD@mail.example.com/v1/inbound/postmark/ENDPOINT_ID

Use the returned username, password and endpoint ID. Postmark sends these as HTTP Basic authentication.
Configure an inbound forwarding domain in Postmark and publish its MX record. AgentBox routes by `OriginalRecipient`;
use your own receiving domain, so this address matches an AgentBox inbox. Forwarding mail to Postmark's generated
`...@inbound.postmarkapp.com` address does not automatically map it back to a different AgentBox address.

Postmark sends parsed JSON rather than full MIME. AgentBox reconstructs MIME with text/HTML, original Message-ID,
References/In-Reply-To, Reply-To and base64 attachments including inline content IDs. Its generated `.eml` is a
reconstruction, not the original wire message. Postmark's delivery `MessageID` provides retry deduplication; it is
also used for a fallback Message-ID when no original header is supplied.

See [Postmark inbound payloads](https://postmarkapp.com/developer/webhooks/inbound-webhook) and
[HTTP Basic authentication](https://postmarkapp.com/developer/webhooks/webhooks-overview).

## Check delivery and operate

Send an email from an external mailbox to an existing AgentBox inbox. Watch it arrive in the console, through
`GET /v1/inboxes/{id}/messages?direction=inbound&wait=30`, or through your registered `message.received` webhook.
The inbound endpoint returns HTTP 200 with counts:

    {"accepted":1,"duplicates":0,"ignored":0}

- `accepted`: durably stored and queued for the worker; processing is asynchronous.
- `duplicates`: a retry already persisted for this endpoint and envelope recipient. Concurrent retries are safe.
- `ignored`: the recipient is unknown, disabled, expired, connected, blocked by receive policy, or outside this organization.

Both accepted and permanently ignored deliveries return 200 so providers stop retrying. Authentication failures
return 401, deleted/unknown endpoints return 404, malformed payloads return 422, and size violations return 413.
Storage or database failures return 5xx and can be retried. Fix configuration errors promptly: retry schedules and
retention differ by provider. Signed timestamps must be within the last 72 hours, with five minutes of future clock
skew allowed, accommodating delayed retries; accepted messages also have persistent database deduplication.

The MIME size limit is `AGENTBOX_MAX_INBOUND_BYTES` (30 MiB by default). The HTTP body has a separate hard bound of
three times that limit plus 1 MiB for URL-encoding/base64 overhead, enforced even without Content-Length. Configure
your reverse proxy to allow the corresponding request size. The worker's existing attachment limit still applies.

Use `GET /v1/inbound-endpoints` with `providers:read` to list endpoints; credentials and signing keys are never
returned. `DELETE /v1/inbound-endpoints/{id}` with `providers:write` revokes an endpoint. Rotate credentials by creating
a replacement, switching the provider to its URL, then deleting the old endpoint. Credentials are encrypted at rest
with `AGENTBOX_APP_SECRET_KEY`. Use HTTPS and keep that key stable and backed up.

Outbound delivery is configured separately: use an SMTP relay on port 587/465 or a supported HTTP sending provider.
These endpoints receive mailbox messages; outbound delivery notifications and VERP bounce processing remain separate
(provider event webhooks and the SMTP edge respectively).
