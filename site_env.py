"""Load private site configuration from the repository's local .env file."""

from __future__ import annotations

import os
from pathlib import Path


ENV_FILE = Path(__file__).with_name(".env")
_loaded = False


def _parse_value(value: str) -> str:
    """Remove optional quotes and comments from a configuration value."""
    value = value.strip()
    if value.startswith(("\"", "'")):
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            return value
        trailing = value[end + 1:].strip()
        if not trailing or trailing.startswith("#"):
            return value[1:end]
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def load() -> None:
    """Load .env once without replacing values already in the environment."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(
                f"invalid .env line {line_number}: expected KEY=VALUE"
            )
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            raise RuntimeError(f"invalid .env line {line_number}: empty variable name")
        value = _parse_value(value)
        os.environ.setdefault(name, value)


def get(name: str, default: str | None = None) -> str | None:
    """Return a configured value, or *default* when it is unset."""
    return os.environ.get(name, default)


def require(name: str) -> str:
    """Return a non-empty configured value or raise a helpful error."""
    value = get(name)
    if value is None or not value.strip():
        raise RuntimeError(
            f"{name} is required; set it in the environment or copy its "
            "example from .env.example into .env"
        )
    return value


load()
