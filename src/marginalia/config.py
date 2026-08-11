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
DEFAULT_SEARCH_ORDER = ("youtube_api", "ytdlp_search")
DEFAULT_DAILY_UNITS = 10_000

# Read only if the config file does not name a key. The file wins because it
# is the thing the user edited on purpose; the variable is the fallback for
# machines where a secret should not sit in a file.
API_KEY_ENV = "YOUTUBE_API_KEY"

# Fallback used when config.example.toml is not on disk (installed package).
_EMBEDDED_EXAMPLE = f"""\
[library]
path = "{DEFAULT_LIBRARY_PATH}"

[providers]
transcript_order = ["ytdlp"]
search_order = ["youtube_api", "ytdlp_search"]
"""


class ConfigError(RuntimeError):
    """The configuration file exists but cannot be used."""


@dataclass(frozen=True)
class Config:
    library_path: Path
    transcript_order: tuple[str, ...]
    search_order: tuple[str, ...]
    youtube_api_key: str | None
    youtube_daily_units: int
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

    providers = data.get("providers") or {}
    return Config(
        library_path=_library_path(data, chosen),
        transcript_order=_order(providers, "transcript_order", DEFAULT_TRANSCRIPT_ORDER, chosen),
        search_order=_order(providers, "search_order", DEFAULT_SEARCH_ORDER, chosen),
        youtube_api_key=_api_key(providers, chosen),
        youtube_daily_units=_daily_units(providers, chosen),
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


def _order(
    providers: dict, key: str, default: tuple[str, ...], config_path: Path
) -> tuple[str, ...]:
    raw = providers.get(key)
    if raw is None:
        return default
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"providers.{key} must be a list of strings in {config_path}")
    if not raw:
        raise ConfigError(f"providers.{key} is empty in {config_path}")
    return tuple(raw)


def _api_key(providers: dict, config_path: Path) -> str | None:
    raw = providers.get("youtube_api_key")
    if raw is not None and not isinstance(raw, str):
        raise ConfigError(f"providers.youtube_api_key must be a string in {config_path}")
    key = (raw or "").strip() or os.environ.get(API_KEY_ENV, "").strip()
    return key or None


def _daily_units(providers: dict, config_path: Path) -> int:
    raw = providers.get("youtube_daily_units")
    if raw is None:
        return DEFAULT_DAILY_UNITS
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ConfigError(
            f"providers.youtube_daily_units must be a non-negative integer in {config_path}"
        )
    return raw


def _repo_root() -> Path:
    # src/marginalia/config.py -> repo root
    return Path(__file__).resolve().parents[2]
