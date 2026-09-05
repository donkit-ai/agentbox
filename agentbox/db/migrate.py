"""Alembic entry points that include migrations shipped by installed extensions."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    from agentbox.extensions import registry

    migrations = ROOT / "alembic"
    ini = ROOT / "alembic.ini"
    if not (migrations / "env.py").is_file():
        migrations = Path(__file__).resolve().parents[1] / "_migrations"
        ini = migrations.with_suffix(".ini")
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(migrations))
    locations = [str(migrations / "versions"), *registry().migration_dirs()]
    cfg.set_main_option("version_locations", " ".join(locations))
    return cfg


def upgrade(revision: str = "heads") -> None:
    command.upgrade(alembic_config(), revision)


def downgrade(revision: str = "base") -> None:
    command.downgrade(alembic_config(), revision)
