"""Load the committed YAML configuration."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
STATE_DIR = REPO_ROOT / "state"


@cache
def load(name: str) -> dict[str, Any]:
    """Read config/<name>.yml. Cached - these files do not change mid-run."""
    with (CONFIG_DIR / f"{name}.yml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def settings() -> dict[str, Any]:
    return load("settings")


def exclusions() -> dict[str, Any]:
    return load("exclusions")


def secret(name: str, *, required: bool = False) -> str | None:
    """Read a credential from the environment.

    Every secret is injected by the runner (GitHub Actions secrets or a local
    .env). Nothing credential-shaped is ever read from the repo.
    """
    value = os.environ.get(name) or None
    if required and not value:
        raise RuntimeError(
            f"{name} is not set. Add it to the repository's Actions secrets."
        )
    return value
