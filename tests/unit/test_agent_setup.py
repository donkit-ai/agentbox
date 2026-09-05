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
