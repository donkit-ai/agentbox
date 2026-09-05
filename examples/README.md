# Three complete email workflows

No model-provider account is required. These examples demonstrate the email/state/approval plumbing and provide
clear places to plug in your agent. Bundled messages are synthetic; external email and attachment text is untrusted.

Start the local stack with `uv run agentbox init --build --mode demo`, then load the generated `agent.env` into
your shell environment (do not print it). Run examples with `uv run --extra dev python examples/...` from the core
checkout, or `python examples/...` when the SDK is already installed. The wizard's SMTP edge is port 8003;
set `AGENTBOX_DEMO_SMTP_PORT` if you selected another base port. No fixtures go to external servers.

## Supplier quote, including process restart

```sh
python examples/supplier_quote.py start --task-id quote-order-123 --state quote.json
# Repeat start with the same state/task ID: no second send.
python examples/supplier_quote.py start --task-id quote-order-123 --state quote.json
python examples/demo_inject.py supplier_reply --reply-to-message msg_FROM_FIRST_OUTPUT
python examples/supplier_quote.py resume --state quote.json
```

The operation ID and request are saved before sending, the returned message ID before creating a durable reply
wait. Resume is a separate process and prints `pending`, `matched` or `expired`. A real supplier reply will also
match if it references the original email. Do not replay states older than the default 24-hour idempotency
retention without reconciling history. Review-profile keys pause sending until human approval.

## Inbound support with human approval

In the console, create a `review` key for a separate support demo inbox and load it as `AGENTBOX_API_KEY` with
that inbox's `AGENTBOX_INBOX_ID`. This script checks the preset before doing any work.

```sh
python examples/demo_inject.py support_request
python examples/support_review.py --consumer support-v1
```

Wait for the inbound worker if the first run reports `idle`. It leases one event, creates a reply with operation
ID derived from that event, then acknowledges the durably created draft. The response stays `pending_approval`;
the agent cannot approve it. A human reviews `/dashboard/approvals` and clicks Approve. In the demo, the outbound
worker then delivers it to Mailpit. The malicious fixture instruction never changes the deterministic draft.
When replacing that draft with an LLM, keep the server-side review gate and treat email as untrusted input.

## Attachment extraction

Use a `readonly` key for a demo inbox:

```sh
python examples/demo_inject.py invoice
python examples/extract_attachments.py
```

After the inbound worker processes it, output includes two CSV rows and totals 1250.00 and 25.00. This example
only parses bounded CSV data; it does not execute spreadsheet formulas, attachment code or external links.

## Tests and recording

`tests/integration/test_examples.py` runs the scripts in separate processes against real API/PostgreSQL/storage,
injects fixtures through SMTP, waits for workers, checks restart deduplication, human approval and CSV extraction.
`scripts/record_demo.py --setup agentbox-local --output dist/demo.cast` records these real workflows with no keys
in the output. See `docs/release.md` for the optional summary video and isolated-stack cleanup.
