"""Small helpers shared by runnable examples. No model API key is needed for the fixtures."""

import json
import os
import tempfile
from pathlib import Path

from agentbox_sdk import AgentBox


def client():
    return AgentBox(
        os.environ["AGENTBOX_API_KEY"], base_url=os.environ.get("AGENTBOX_API_URL", "http://localhost:8000")
    )


def inbox_id():
    return os.environ["AGENTBOX_INBOX_ID"]


def save_state(path: Path, data):
    # Atomic replacement: a crash leaves either the previous or complete new state, not partial JSON.
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def output(data):
    print(json.dumps(data, indent=2))
