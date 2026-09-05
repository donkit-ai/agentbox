"""Record real fixture workflows on an isolated `agentbox init --mode demo` stack.

Credentials are read privately, never included in the recording. No external email is sent.
Writes an asciinema v2 recording (idle gaps compressed) and, optionally, a 30-second summary video.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from agentbox_sdk import AgentBox

ROOT = Path(__file__).resolve().parents[1]


class Recording:
    def __init__(self, output, secrets):
        self.file = output.open("x")
        self.file.write(
            json.dumps(
                {"version": 2, "width": 100, "height": 32, "title": "AgentBox: real local demo; idle gaps compressed"}
            )
            + "\n"
        )
        self.secrets = secrets
        self.last, self.position = time.monotonic(), 0.0

    def emit(self, value):
        value = str(value)
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        now = time.monotonic()
        self.position += min(max(now - self.last, 0.1), 2)
        self.last = now
        self.file.write(json.dumps([round(self.position, 3), "o", value.replace("\n", "\r\n") + "\r\n"]) + "\n")
        self.file.flush()
        print(value)

    def close(self):
        self.file.write(json.dumps([self.position + 3, "o", "\r\n"]) + "\n")
        self.file.close()


def video(pages, destination):
    """Render captions derived only from checked results; this is a summary, not a desktop capture."""
    if not shutil.which("ffmpeg"):
        raise SystemExit("Install ffmpeg to use --video, or omit that option for the terminal recording.")
    font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
    if not font.is_file():
        raise SystemExit("--video requires the DejaVu Sans Mono font; the .cast recording is still available.")
    with tempfile.TemporaryDirectory(prefix="agentbox-demo-video-") as folder:
        filters = []
        for index, (title, body) in enumerate(pages):
            lines = ["AGENTBOX 0.2.0", "Local demo / verified output summary", "", title, ""]
            for line in body.splitlines():
                lines.extend(textwrap.wrap(line, 73) or [""])
            lines.extend(
                [
                    "",
                    "Mailpit + synthetic replies. No internet delivery.",
                    "No mailbox passwords or API keys on screen.",
                ]
            )
            caption = Path(folder) / f"page-{index}.txt"
            caption.write_text("\n".join(lines))
            filters.append(
                f"drawtext=fontfile={font}:textfile={caption}:expansion=none:"
                f"fontcolor=0xe4ecf5:fontsize=26:line_spacing=12:x=64:y=55:"
                f"enable='gte(t,{index * 6})*lt(t,{(index + 1) * 6})'"
            )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x101824:s=1280x720:r=10",
                "-t",
                str(len(pages) * 6),
                "-vf",
                ",".join(filters),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True, help="Private directory created by agentbox init")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/demo.cast")
    parser.add_argument("--video", type=Path, help="Optional verified-results summary MP4; needs ffmpeg")
    args = parser.parse_args()
    environment = dict(
        line.split("=", 1) for line in (args.setup / "agent.env").read_text().splitlines() if "=" in line
    )
    settings = dict(line.split("=", 1) for line in (args.setup / ".env").read_text().splitlines() if "=" in line)
    operator = json.loads((args.setup / "operator.json").read_text())
    base = environment["AGENTBOX_API_URL"]
    if urlsplit(base).hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("Only an isolated local demo stack is supported.")
    if args.output.exists() or (args.video and args.video.exists()):
        raise SystemExit("Choose new output paths; existing recordings are never overwritten.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    record = Recording(args.output, [operator["api_key"], environment["AGENTBOX_API_KEY"]])
    pages = []
    env = {**os.environ, **environment, "AGENTBOX_DEMO_SMTP_PORT": str(int(settings["AGENTBOX_HTTP_PORT"]) + 3)}

    def run(script, current_env, *arguments):
        record.emit("$ python examples/" + script + " " + " ".join(arguments))
        result = subprocess.run(
            [sys.executable, str(ROOT / "examples" / script), *arguments],
            env=current_env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode:
            record.emit(result.stderr)
            raise RuntimeError("Example failed; inspect the redacted recording.")
        record.emit(result.stdout.strip())
        return json.loads(result.stdout)

    def inject(fixture, current_env, *arguments):
        since = datetime.now(UTC).isoformat()
        run("demo_inject.py", current_env, fixture, *arguments)
        with AgentBox(current_env["AGENTBOX_API_KEY"], base_url=base) as box:
            assert box.messages.wait_for(current_env["AGENTBOX_INBOX_ID"], since=since, timeout=30)

    try:
        with (
            AgentBox(operator["api_key"], base_url=base) as admin,
            tempfile.TemporaryDirectory(prefix="agentbox-demo-state-") as state,
        ):
            inbox = admin.inboxes.get(environment["AGENTBOX_INBOX_ID"])
            if inbox["provider_mode"] != "managed" or not inbox["email"].endswith("@agentbox.local"):
                raise SystemExit("A managed @agentbox.local inbox is required.")
            record.emit("AgentBox: real local demo; idle gaps compressed. Credentials are not recorded.")
            record.emit("$ docker compose exec api agentbox doctor --roundtrip [local organization/inbox]")
            checked = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--project-directory",
                    str(args.setup.resolve()),
                    "exec",
                    "-T",
                    "api",
                    "agentbox",
                    "doctor",
                    "--roundtrip",
                    "--organization-id",
                    operator["organization_id"],
                    "--inbox-id",
                    inbox["id"],
                ],
                capture_output=True,
                text=True,
                timeout=80,
            )
            record.emit(checked.stdout)
            if checked.returncode:
                record.emit(checked.stderr)
                raise RuntimeError("Local-only route check failed; no examples were run.")
            pages.append(
                (
                    "1 / Local stack is healthy",
                    "PostgreSQL + object storage + live worker\n"
                    "Outbound message accepted by Mailpit.\nSynthetic SMTP reply received and stored.\n"
                    "Generated secrets stay in private files.",
                )
            )

            quote_args = ("--state", str(Path(state) / "quote.json"), "--task-id", "demo-" + uuid4().hex[:12])
            sent = run("supplier_quote.py", env, "start", *quote_args)
            repeated = run("supplier_quote.py", env, "start", *quote_args)
            assert sent == repeated and sent["status"] == "pending"
            pages.append(
                (
                    "2 / Send once, resume safely",
                    f"First call: {sent['status']}\n"
                    f"Repeated call: same message ID\n{sent['message_id']}\n"
                    "Request and operation ID saved before sending.\nReply wait survives process restarts.",
                )
            )
            inject("supplier_reply", env, "--reply-to-message", sent["message_id"])
            resumed = run("supplier_quote.py", env, "resume", "--state", quote_args[1])
            assert resumed["status"] == "matched"
            pages.append(("3 / Reply matched after restart", resumed["untrusted_supplier_reply"].strip()))

            def scoped(profile):
                created = admin.inboxes.create(display_name="Recorded " + profile)
                key = admin.api_keys.create("demo-" + profile, permission_profile=profile, inbox_ids=[created["id"]])
                record.secrets.append(key["api_key"])
                record.emit(f"Operator created {profile} key scoped to {created['id']}; key value omitted.")
                return {**env, "AGENTBOX_API_KEY": key["api_key"], "AGENTBOX_INBOX_ID": created["id"]}, key["id"]

            support_env, support_key = scoped("review")
            inject("support_request", support_env)
            draft = run("support_review.py", support_env)
            assert draft["status"] == "pending_approval"
            assert run("support_review.py", support_env)["status"] == "idle"
            approved = admin.messages.approve(draft["draft_message_id"])
            assert approved["status"] == "queued"
            record.emit("Demo operator approved the draft. Agent key cannot approve its own messages.")
            pages.append(
                (
                    "4 / Human review is enforced",
                    "Agent reply: pending_approval\n"
                    "Repeated event consumer: idle (acknowledged)\n"
                    "Demo operator approval: queued\n"
                    "The malicious email fixture cannot bypass the review gate.",
                )
            )
            admin.api_keys.revoke(support_key)

            readonly_env, readonly_key = scoped("readonly")
            inject("invoice", readonly_env)
            extracted = run("extract_attachments.py", readonly_env)
            totals = [row["total"] for row in extracted["invoices"][0]["untrusted_rows"]]
            assert totals == ["1250.00", "25.00"]
            pages.append(
                (
                    "5 / Attachments as untrusted data",
                    "Inbox-scoped read-only key\n"
                    "Bounded CSV parsing; no code or formulas executed\n"
                    f"Verified row totals: {', '.join(totals)}\nThree workflows completed with real API calls.",
                )
            )
            admin.api_keys.revoke(readonly_key)
            record.emit("All three workflows passed. Test mailboxes remain in this isolated stack.")
    finally:
        record.close()
    if args.video:
        args.video.parent.mkdir(parents=True, exist_ok=True)
        video(pages, args.video)


if __name__ == "__main__":
    main()
