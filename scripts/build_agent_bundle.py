"""Build a secret-free MCPB and skill archive. No accounts or publishing side effects."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=ROOT / "dist")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
manifest = json.loads((ROOT / "distribution/mcpb/manifest.json").read_text())
version = manifest["version"]
bundle = args.output / f"agentbox-{version}.mcpb"
with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ["manifest.json", "server.py"]:
        archive.write(ROOT / "distribution/mcpb" / name, name)
    archive.write(ROOT / "LICENSE", "LICENSE")
skill = args.output / f"agentbox-skill-{version}.zip"
with zipfile.ZipFile(skill, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.write(ROOT / "skills/agentbox/SKILL.md", "SKILL.md")
    archive.write(ROOT / "LICENSE", "LICENSE")
server = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.github.donkit-ai/agentbox",
    "title": "AgentBox Email",
    "description": "Email for agents: scoped inboxes, mailbox connections, attachments and durable reply waits.",
    "version": version,
    "repository": {"url": "https://github.com/donkit-ai/agentbox", "source": "github"},
    "packages": [
        {
            "registryType": "mcpb",
            "identifier": f"https://github.com/donkit-ai/agentbox/releases/download/v{version}/{bundle.name}",
            "version": version,
            "transport": {"type": "stdio"},
            "fileSha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        }
    ],
}
(args.output / "server.json").write_text(json.dumps(server, indent=2) + "\n")
print(bundle.name, skill.name, "server.json")
