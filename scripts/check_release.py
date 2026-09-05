"""Fail before packaging if Python, TS, MCPB and optional Git tag versions disagree."""

import json
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
assert tomllib.loads((ROOT / "sdk/python/pyproject.toml").read_text())["project"]["version"] == version
assert json.loads((ROOT / "sdk/typescript/package.json").read_text())["version"] == version
assert json.loads((ROOT / "distribution/mcpb/manifest.json").read_text())["version"] == version
ref = os.environ.get("GITHUB_REF", "")
if ref.startswith("refs/tags/"):
    assert ref == "refs/tags/v" + version, f"Tag {ref} does not match {version}"
print("Release versions agree:", version)
