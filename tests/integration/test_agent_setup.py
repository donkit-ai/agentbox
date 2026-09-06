"""Compose must restore saved credentials literally, including shell-significant characters."""
import os
import subprocess

from agentbox.onboarding import compose_env, private_file


async def test_compose_restores_saved_mail_credentials(tmp_path, db):
    values = {
        "AGENTBOX_GOOGLE_CLIENT_ID": "client-id",
        "AGENTBOX_GOOGLE_CLIENT_SECRET": "s3cr'et-${HOME}-$HOME-$$-\\path",
        "AGENTBOX_OUTBOUND_SMTP_PASSWORD": 'trailing\\',
        "CHECK_QUOTES": "quote\\'and\\",
        "CHECK_UNICODE": 'кириллица-"quotes"-#',
    }
    private_file(tmp_path / ".env", compose_env(values))
    private_file(tmp_path / "compose.yaml", "services:\n  api:\n    image: alpine:3.21\n    environment:\n"
                 + "".join("      " + key + ": ${" + key + "}\n" for key in values))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTBOX_", "COMPOSE_"))}
    compose = ["docker", "compose", "--project-directory", str(tmp_path)]
    try:
        result = subprocess.run([*compose, "run", "--rm", "-T", "--no-deps", "api", "env"],
                                check=True, capture_output=True, text=True, env=env)
        actual = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.split("=", 1)[0] in values)
        assert actual == values
    finally:
        subprocess.run([*compose, "down"], check=True, capture_output=True, env=env)
