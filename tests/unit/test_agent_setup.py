import json
import stat
import tomllib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentbox.cli import app
from agentbox.onboarding import private_file


def test_init_refuses_existing_files(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("user data")
    result = CliRunner().invoke(app, ["init", "--directory", str(tmp_path)])
    assert result.exit_code != 0 and "not empty" in result.output
    assert marker.read_text() == "user data"


def test_private_file_is_exclusive_and_owner_only(tmp_path):
    file = tmp_path / "agent.env"
    private_file(file, "test-secret")
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        private_file(file, "replacement")
    assert file.read_text() == "test-secret"


def test_mcp_templates_have_correct_client_shapes():
    root = Path(__file__).resolve().parents[2] / "docs/mcp"
    desktop = json.loads((root / "claude-cursor.json").read_text())["mcpServers"]["agentbox"]
    claw = json.loads((root / "openclaw.json").read_text())["mcp"]["servers"]["agentbox"]
    hermes = yaml.safe_load((root / "hermes.yaml").read_text())["mcp_servers"]["agentbox"]
    codex = tomllib.loads((root / "codex.toml").read_text())["mcp_servers"]["agentbox"]
    for entry in [desktop, claw, hermes, codex]:
        assert entry["command"] == "uvx" and entry["args"][-1] == "agentbox-mcp"
        assert "@v0.2.0#subdirectory=sdk/python" in entry["args"][1]
    remote = tomllib.loads((root / "codex-http.toml").read_text())["mcp_servers"]["agentbox"]
    assert remote["url"].endswith("/mcp") and remote["bearer_token_env_var"] == "AGENTBOX_API_KEY"


@pytest.fixture
def clean_mail_environment(monkeypatch):
    from agentbox.onboarding import MAIL_SETTINGS

    for key in MAIL_SETTINGS:
        monkeypatch.delenv(key, raising=False)


def test_missing_mailbox_setup_fails_before_docker(tmp_path, monkeypatch, clean_mail_environment):
    import agentbox.onboarding as setup

    def unexpected(*args, **kwargs):
        raise AssertionError("Docker must not run before mailbox configuration is validated")

    monkeypatch.setattr(setup.subprocess, "run", unexpected)
    target = tmp_path / "mail"
    result = CliRunner().invoke(app, ["init", "--mode", "mailbox", "--directory", str(target)])
    assert result.exit_code != 0 and "--config mailbox.env" in result.output and "--app-passwords" in result.output
    assert not target.exists()


def test_mail_config_filters_foreign_deployment_and_preserves_secrets(tmp_path, clean_mail_environment):
    from agentbox.onboarding import compose_env, mail_configuration

    config = tmp_path / "mailbox.env"
    config.write_text("AGENTBOX_GOOGLE_CLIENT_ID=client-id\nAGENTBOX_GOOGLE_CLIENT_SECRET='literal-$SECRET'\n"
                      "AGENTBOX_EDITION=cloud\nPOSTGRES_PASSWORD=foreign\nAGENTBOX_APP_SECRET_KEY=foreign\n")
    settings = mail_configuration(config, mode="mailbox", provider="gmail", app_passwords=False)
    assert settings == {"AGENTBOX_GOOGLE_CLIENT_ID": "client-id", "AGENTBOX_GOOGLE_CLIENT_SECRET": "literal-$SECRET"}
    saved = tmp_path / "generated.env"
    private_file(saved, compose_env(settings))
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_demo_ignores_ambient_mail_settings(monkeypatch):
    from agentbox.onboarding import mail_configuration

    monkeypatch.setenv("AGENTBOX_OUTBOUND_SMTP_HOST", "smtp.real.example")
    monkeypatch.setenv("AGENTBOX_GOOGLE_CLIENT_SECRET", "do-not-import")
    assert mail_configuration(None, mode="demo", provider="gmail", app_passwords=False) == {}


def test_app_password_is_an_explicit_self_hosted_choice(clean_mail_environment):
    from agentbox.onboarding import mail_configuration

    assert mail_configuration(None, mode="mailbox", provider="yandex360", app_passwords=True) == {
        "AGENTBOX_ALLOW_APP_PASSWORD_CONNECTIONS": "true"}
    result = CliRunner().invoke(app, ["init", "--mode", "mailbox", "--provider", "m365", "--app-passwords"])
    assert result.exit_code != 0 and "requires OAuth" in result.output
