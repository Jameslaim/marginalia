"""Configuration loading.

Resolution order:

1. ``MARGINALIA_CONFIG`` environment variable
2. ``./config.toml``
3. ``~/.marginalia/config.toml``

On first run, when none of those exist, one is created from
``config.example.toml`` and the CLI says so.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["Config", "ConfigError", "load_config", "example_text"]

DEFAULT_LIBRARY_PATH = "C:/youtube-agent/marginalia-library"
DEFAULT_TRANSCRIPT_ORDER = ("ytdlp",)

# Fallback used when config.example.toml is not on disk (installed package).
_EMBEDDED_EXAMPLE = f"""\
[library]
path = "{DEFAULT_LIBRARY_PATH}"

[providers]
transcript_order = ["ytdlp"]
"""


class ConfigError(RuntimeError):
    """The configuration file exists but cannot be used."""


@dataclass(frozen=True)
class Config:
    library_path: Path
    transcript_order: tuple[str, ...]
    path: Path
    created: bool


def example_text() -> str:
    """Contents for a new config file."""
    example = _repo_root() / "config.example.toml"
    if example.is_file():
        try:
            return example.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable example is not fatal
            log.warning("could not read %s", example)
    return _EMBEDDED_EXAMPLE


def load_config() -> Config:
    candidates = _candidates()
    chosen = next((path for path in candidates if path.is_file()), None)
    created = False

    if chosen is None:
        chosen = candidates[-1]
        try:
            chosen.parent.mkdir(parents=True, exist_ok=True)
            chosen.write_text(example_text(), encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"could not create config at {chosen}: {exc}") from exc
        created = True

    try:
        # utf-8-sig: Windows editors write a BOM, and tomllib rejects one.
        data = tomllib.loads(chosen.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config at {chosen}: {exc}") from exc

    return Config(
        library_path=_library_path(data, chosen),
        transcript_order=_transcript_order(data, chosen),
        path=chosen,
        created=created,
    )


def _candidates() -> list[Path]:
    override = os.environ.get("MARGINALIA_CONFIG")
    if override:
        return [Path(override).expanduser()]
    return [Path.cwd() / "config.toml", Path.home() / ".marginalia" / "config.toml"]


def _library_path(data: dict, config_path: Path) -> Path:
    raw = (data.get("library") or {}).get("path") or DEFAULT_LIBRARY_PATH
    if not isinstance(raw, str):
        raise ConfigError(f"library.path must be a string in {config_path}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return Path(os.path.normpath(path))


def _transcript_order(data: dict, config_path: Path) -> tuple[str, ...]:
    raw = (data.get("providers") or {}).get("transcript_order")
    if raw is None:
        return DEFAULT_TRANSCRIPT_ORDER
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"providers.transcript_order must be a list of strings in {config_path}")
    if not raw:
        raise ConfigError(f"providers.transcript_order is empty in {config_path}")
    return tuple(raw)


def _repo_root() -> Path:
    # src/marginalia/config.py -> repo root
    return Path(__file__).resolve().parents[2]
