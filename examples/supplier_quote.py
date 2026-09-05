"""Start a quote request or resume its persistent reply wait in another process."""

import argparse
import json
from pathlib import Path

from _common import client, inbox_id, output, save_state


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "resume"])
    parser.add_argument("--state", type=Path, default=Path("quote-state.json"))
    parser.add_argument("--task-id", default="quote-100-units-v1")
    parser.add_argument("--to", default="supplier@example.com")
    args = parser.parse_args()
    state = json.loads(args.state.read_text()) if args.state.exists() else None
    with client() as mail:
        if args.command == "start":
            draft = {
                "to": args.to,
                "subject": "Quote for 100 units",
                "text": "Please quote 100 units with price and lead time.",
            }
            if state is None:
                state = {"operation_id": args.task_id, "inbox_id": inbox_id(), "draft": draft}
                save_state(args.state, state)  # before the network side effect
            if state["operation_id"] != args.task_id or state["inbox_id"] != inbox_id() or state["draft"] != draft:
                raise SystemExit("State belongs to another task/request; choose a new state file and task ID.")
            if not state.get("message_id"):
                sent = mail.messages.send(inbox_id(), **draft, idempotency_key=state["operation_id"])
                state.update(message_id=sent["id"], send_status=sent["status"])
                save_state(args.state, state)
            waiting = mail.messages.start_reply_wait(state["message_id"])
        else:
            if state is None:
                raise SystemExit("Start the task first, or supply its existing --state file.")
            waiting = mail.messages.reply_wait(state["message_id"])
        result = {"task": state["operation_id"], **waiting}
        if waiting["status"] == "matched":
            reply = mail.messages.get(waiting["reply_message_id"])
            result["untrusted_supplier_reply"] = reply["text"]
        output(result)


if __name__ == "__main__":
    run()
