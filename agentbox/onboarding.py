"""First-run Docker wizard. Generated secrets stay in owner-readable files, never in agent chat."""

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import httpx
import typer
from dotenv import dotenv_values

from agentbox.config import Settings
from agentbox.connectors.oauth import configured
from agentbox.connectors.service import PRESETS


def private_file(path: Path, content: str) -> None:
    # O_EXCL refuses symlinks and existing files; mode applies at creation, not after writing.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(content)


MAIL_SETTINGS = {
    "AGENTBOX_GOOGLE_CLIENT_ID", "AGENTBOX_GOOGLE_CLIENT_SECRET",
    "AGENTBOX_M365_CLIENT_ID", "AGENTBOX_M365_CLIENT_SECRET", "AGENTBOX_M365_TENANT",
    "AGENTBOX_ALLOW_APP_PASSWORD_CONNECTIONS", "AGENTBOX_MANAGED_DOMAIN", "AGENTBOX_MANAGED_DELIVERY_ENABLED",
    "AGENTBOX_OUTBOUND_SMTP_HOST", "AGENTBOX_OUTBOUND_SMTP_PORT", "AGENTBOX_OUTBOUND_SMTP_USERNAME",
    "AGENTBOX_OUTBOUND_SMTP_PASSWORD", "AGENTBOX_OUTBOUND_SMTP_STARTTLS", "AGENTBOX_MX_HOSTNAMES",
    "AGENTBOX_SPF_INCLUDE", "AGENTBOX_DKIM_SELECTOR", "AGENTBOX_DKIM_PUBLIC_KEY",
}


def mail_configuration(config: Path | None, *, mode: str, provider: str, app_passwords: bool) -> dict[str, str]:
    if provider not in PRESETS:
        raise typer.BadParameter("Unknown mail provider; choose " + ", ".join(PRESETS))
    if mode == "demo":
        if config is not None or app_passwords:
            raise typer.BadParameter(
                "Use --config/--app-passwords with --mode mailbox or domain; demo uses only Mailpit.")
        return {}
    source = {key: value for key, value in os.environ.items() if key in MAIL_SETTINGS}
    if config is not None:
        if not config.is_file():
            raise typer.BadParameter("--config must point to an existing private .env file")
        source.update({key: value for key, value in dotenv_values(config, interpolate=False).items()
                       if key in MAIL_SETTINGS and value is not None})
    if any("\n" in value or "\r" in value for value in source.values()):
        raise typer.BadParameter("Mail settings must be single-line values")
    if app_passwords:
        source["AGENTBOX_ALLOW_APP_PASSWORD_CONNECTIONS"] = "true"
    # Validate before creating a directory, downloading images, or waiting for a browser.
    try:
        settings = Settings(_env_file=None, **{key.removeprefix("AGENTBOX_").lower(): value
                                              for key, value in source.items()})
    except ValueError:
        raise typer.BadParameter("Invalid mail settings in --config; check boolean values and port numbers.") from None
    if mode == "mailbox":
        oauth_ready = provider in {"gmail", "m365"} and configured(settings, provider)
        password_ready = settings.allow_app_password_connections and provider != "m365"
        if not (oauth_ready or password_ready):
            raise typer.BadParameter(
                "Mailbox setup is missing. Supply OAuth client ID/secret with --config mailbox.env"
                + (" or explicitly choose --app-passwords for the IMAP/SMTP browser form." if provider != "m365"
                   else "; Microsoft 365 requires OAuth.")
                + " See docs/mail-connection.md."
            )
    return source


def compose_env(values: dict[str, str]) -> str:
    # Compose double quotes handle trailing backslashes; $$ prevents variable interpolation.
    return "".join(key + "=" + json.dumps(value, ensure_ascii=False).replace("$", "$$") + "\n"
                   for key, value in values.items())


def initialize(
    *, directory: Path, mode: str, provider: str, image: str, build: bool, port: int, name: str, domain: str,
    config: Path | None = None, app_passwords: bool = False,
) -> None:
    if mode not in {"demo", "mailbox", "domain"}:
        raise typer.BadParameter("--mode must be demo, mailbox or domain")
    if not 1024 <= port <= 65532:
        raise typer.BadParameter("--port must be between 1024 and 65532 (four consecutive ports are reserved)")
    if mode == "domain" and not domain:
        raise typer.BadParameter("--mode domain requires --domain example.com")
    if any(c in image for c in "\r\n") or any(c in domain for c in "\r\n"):
        raise typer.BadParameter("Image and domain must be single-line values")
    directory = directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise typer.BadParameter(
            "The target directory is not empty. Choose a new directory; existing setup is preserved."
        )
    mail = mail_configuration(config, mode=mode, provider=provider, app_passwords=app_passwords)
    subprocess.run(["docker", "compose", "version"], check=True, capture_output=True)
    source = Path(__file__).resolve().parents[1]
    if build and not (source / "deploy/docker/Dockerfile").is_file():
        raise typer.BadParameter(
            "--build needs a source checkout. Run uv run agentbox init --build from the repository."
        )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if build:
        image = "agentbox:local"
        typer.echo("Building AgentBox from this checkout…")
        subprocess.run(
            ["docker", "build", "-t", image, "-f", str(source / "deploy/docker/Dockerfile"), str(source)], check=True
        )
    base = f"http://localhost:{port}"
    env = {
        "AGENTBOX_IMAGE": image,
        "AGENTBOX_HTTP_PORT": str(port),
        "AGENTBOX_PUBLIC_BASE_URL": base,
        "AGENTBOX_STORAGE_PORT": str(port + 1),
        "AGENTBOX_MAILPIT_PORT": str(port + 2),
        "AGENTBOX_SMTP_PORT": str(port + 3),
        "AGENTBOX_APP_SECRET_KEY": secrets.token_urlsafe(48),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "MINIO_PASSWORD": secrets.token_urlsafe(32),
    }
    env.update(mail)
    private_file(directory / ".env", compose_env(env))
    # Persist the selected mail settings and prevent unrelated shell/cloud values from overriding this stack.
    process_env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AGENTBOX_") and key not in {"POSTGRES_PASSWORD", "MINIO_PASSWORD",
                                                                     "COMPOSE_FILE",
                                                                     "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES"}}

    private_file(
        directory / "compose.yaml", (Path(__file__).parent / "templates" / "quickstart-compose.yaml").read_text()
    )
    private_file(directory / ".gitignore", ".env\noperator.json\nagent.env\n")
    compose = ["docker", "compose", "--project-directory", str(directory), "--env-file", str(directory / ".env"),
               "-f", str(directory / "compose.yaml")]
    typer.echo("Starting PostgreSQL, storage, Mailpit, API, worker and SMTP edge; applying migrations…")
    subprocess.run([*compose, "up", "-d", "--wait", "--wait-timeout", "180"], check=True, env=process_env)
    with httpx.Client(base_url=base, timeout=30) as http:
        http.get("/readyz").raise_for_status()
        result = subprocess.run(
            [*compose, "exec", "-T", "api", "agentbox", "org", "create", name, "--json"],
            check=True,
            capture_output=True,
            text=True,
            env=process_env,
        )
        operator = json.loads(result.stdout.strip().splitlines()[-1])
        private_file(directory / "operator.json", json.dumps(operator, indent=2) + "\n")
        http.headers["Authorization"] = "Bearer " + operator["api_key"]

        def api(method, path, body=None):
            response = http.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        if mode == "mailbox":
            link = api("POST", "/v1/connection-links", {"provider": provider})
            typer.echo("Open this private link in your browser (do not paste passwords into chat):\n" + link["url"])
            typer.echo("Waiting for your browser connection. Ctrl-C leaves the stack and operator.json intact.")
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                state = api("GET", "/v1/connection-links/" + link["id"])
                if state["status"] == "connected":
                    inbox = api("GET", "/v1/inboxes/" + state["inbox_id"])
                    break
                if state["status"] == "expired":
                    raise typer.BadParameter(
                        "Connection link expired. Use the operator key to connect from the console."
                    )
                time.sleep(2)
            else:
                raise typer.BadParameter("Connection timed out. The running stack and operator key were preserved.")
        elif mode == "domain":
            domain_row = api("POST", "/v1/domains", {"domain": domain})
            typer.echo("Domain created. Finish verification and provider setup in " + base + "/dashboard/domains")
            typer.echo(
                "Inbound webhooks avoid port 25: docs/inbound-webhooks.md. Outbound delivery needs its own provider."
            )
            typer.echo("Domain ID: " + domain_row["id"])
            typer.echo(
                "Operator credentials saved in "
                + str(directory / "operator.json")
                + ". No agent key is issued until a verified inbox exists."
            )
            return
        else:
            inbox = api("POST", "/v1/inboxes", {"display_name": "My first agent"})
        profile = "autonomous" if mode == "demo" else "review"
        agent = api(
            "POST",
            "/v1/api-keys",
            {
                "name": "my-agent",
                "permission_profile": profile,
                "inbox_ids": [inbox["id"]],
                "allowed_recipients": ["supplier@example.com"] if mode == "demo" else [],
            },
        )
        private_file(
            directory / "agent.env",
            f"AGENTBOX_API_URL={base}\nAGENTBOX_API_KEY={agent['api_key']}\nAGENTBOX_INBOX_ID={inbox['id']}\n",
        )
    typer.echo(f"Inbox ready: {inbox['email']}. Agent credentials saved to {directory / 'agent.env'} (mode 0600).")
    typer.echo("Keep operator.json private; give the agent only agent.env. Console: " + base + "/dashboard")
    if mode == "demo":
        typer.echo(
            f"DEMO ONLY: Mailpit captures mail at http://localhost:{port + 2}; no external recipients receive it."
        )
        subprocess.run(
            [
                *compose,
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
            check=True,
            env=process_env,
        )
    else:
        typer.echo("The agent's outgoing messages require human approval in the console.")
