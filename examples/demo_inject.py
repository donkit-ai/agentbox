"""Inject a bundled fixture at a LOCAL demo SMTP edge. Refuses real-domain inboxes and remote hosts."""

import argparse
import os
import smtplib
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

from _common import client, inbox_id, output


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", choices=["supplier_reply", "support_request", "invoice"])
    parser.add_argument("--reply-to-message")
    args = parser.parse_args()
    host = os.environ.get("AGENTBOX_DEMO_SMTP_HOST", "localhost")
    port = int(os.environ.get("AGENTBOX_DEMO_SMTP_PORT", "8003"))
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Fixture injection is local only.")
    with client() as mail:
        inbox = mail.inboxes.get(inbox_id())
        if inbox["provider_mode"] != "managed" or not inbox["email"].endswith("@agentbox.local"):
            raise SystemExit("Fixture injection requires a managed @agentbox.local demo inbox.")
        message = BytesParser(policy=policy.SMTP).parsebytes(
            (Path(__file__).parent / "fixtures" / (args.fixture + ".eml")).read_bytes()
        )
        message["To"], message["Message-ID"] = inbox["email"], f"<{uuid4()}@example.com>"
        if args.reply_to_message:
            sent = mail.messages.get(args.reply_to_message)
            if sent["inbox_id"] != inbox["id"]:
                raise SystemExit("The sent message belongs to another inbox.")
            message["In-Reply-To"] = sent["internet_message_id"]
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.send_message(message)
        output({"injected": args.fixture, "inbox": inbox["email"], "mode": "local-demo"})


if __name__ == "__main__":
    run()
