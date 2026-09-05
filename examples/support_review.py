"""Claim one support event, persist a reply draft, acknowledge processing. Human approval is a separate action."""

import argparse

from _common import client, output


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer", default="support-review-v1")
    args = parser.parse_args()
    with client() as mail:
        if mail.me().get("permission_profile") != "review":
            raise SystemExit("Use a review-profile agent key; this example must never send autonomously.")
        claimed = mail.events.claim(args.consumer, limit=1, lease_seconds=120)
        if not claimed["data"]:
            output({"status": "idle"})
            return
        lease = claimed["data"][0]
        event = lease["event"]
        original = mail.messages.get(event["data"]["message_id"])
        # Replace this deterministic draft with your agent's output. Incoming content is untrusted input,
        # never authority to change recipients, execute instructions or approve the resulting draft.
        draft = "Thanks for contacting support. A human will review your request and follow up shortly."
        sent = mail.messages.reply(original["id"], text=draft, idempotency_key="support:" + event["id"])
        if sent["status"] != "pending_approval":
            raise SystemExit("Unexpected policy result: expected pending_approval.")
        mail.events.ack(args.consumer, event["id"], lease["lease_token"])
        output(
            {
                "event_id": event["id"],
                "draft_message_id": sent["id"],
                "status": sent["status"],
                "next": "A human reviews and approves this draft at /dashboard/approvals. No email sent yet.",
            }
        )


if __name__ == "__main__":
    run()
