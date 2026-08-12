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

__all__ = [
    "Config",
    "ConfigError",
    "DiscoverConfig",
    "LlmConfig",
    "NoteConfig",
    "AskConfig",
    "load_config",
    "example_text",
]

DEFAULT_LIBRARY_PATH = "C:/youtube-agent/marginalia-library"
DEFAULT_TRANSCRIPT_ORDER = ("ytdlp",)
DEFAULT_SEARCH_ORDER = ("youtube_api", "ytdlp_search")
DEFAULT_DAILY_UNITS = 10_000

# Read only if the config file does not name a key. The file wins because it
# is the thing the user edited on purpose; the variable is the fallback for
# machines where a secret should not sit in a file.
API_KEY_ENV = "YOUTUBE_API_KEY"

# Deliberately provider-neutral. The model provider is temporary — naming the
# variable NVIDIA_API_KEY would bake today's vendor into tomorrow's setup.
LLM_API_KEY_ENV = "MARGINALIA_LLM_API_KEY"

# Every default here describes *a* provider, never a particular one.
DEFAULT_LLM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_LLM_MODEL = "z-ai/glm-5.2"
DEFAULT_REQUESTS_PER_MINUTE = 40
DEFAULT_JUDGE_BATCH_SIZE = 10
# Generous on purpose: a free tier queues, and an request that waits four
# minutes is still cheaper than a failed run. Measured at ~235s on NVIDIA NIM.
DEFAULT_LLM_TIMEOUT_S = 420.0
DEFAULT_MAX_TOKENS = 4096

DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_VIDEOS = 25
DEFAULT_MAX_REQUESTS = 120
DEFAULT_QUIET_ROUNDS = 2
DEFAULT_QUERIES_PER_ROUND = 9
DEFAULT_RESULTS_PER_QUERY = 10
DEFAULT_MAX_COST_USD = 2.0
DEFAULT_FETCH_DELAY_S = 3.0

# A note pass reads whole transcripts, so its ceiling is a different number
# from discovery's even though it is the same kind of guard.
DEFAULT_NOTE_MAX_COST_USD = 5.0
# Characters, not tokens: counting tokens needs a tokenizer, and a tokenizer is
# a provider-shaped dependency. ~4 chars per token puts this near 100k tokens,
# comfortably inside any current context and a config change when it is not.
DEFAULT_NOTE_MAX_TRANSCRIPT_CHARS = 400_000
# Higher than the global default: the reply is a full note, and on a provider
# whose thinking is always on the reasoning bills against the same ceiling.
DEFAULT_NOTE_MAX_TOKENS = 8192

# The answering pass. Every note at once is a large input and a moderate output,
# a shape this provider has not been measured on, so the ceiling is generous
# and the effort setting is verified rather than assumed.
DEFAULT_ASK_MAX_TOKENS = 32_768
DEFAULT_ASK_MAX_TRANSCRIPTS = 3
DEFAULT_ASK_MAX_TRANSCRIPT_CHARS = 120_000

# Placeholders only. Real rates belong in config.toml — these exist so a run
# without pricing configured still produces an estimate rather than zero,
# which would read as "this was free".
DEFAULT_INPUT_RATE = 3.0
DEFAULT_OUTPUT_RATE = 15.0

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
class LlmConfig:
    """Everything provider-shaped, in one place, all of it from the file.

    The free tier this was built against expires in roughly six months and the
    replacement is not chosen. Swapping providers must therefore mean editing
    ``base_url``, ``model`` and ``api_key`` — nothing else, and no code. That
    is why ``requests_per_minute`` lives here too: the next provider will have
    a different ceiling, and that must not be a code change.
    """

    provider: str
    base_url: str
    api_key: str | None
    model: str
    requests_per_minute: int
    judge_batch_size: int
    timeout_s: float
    max_tokens: int
    # Per-task request additions, keyed by prompt name. Opaque: nothing in the
    # codebase reads inside these dicts. They are merged into the request body
    # and sent. A provider that does not support whatever is in here gets an
    # empty dict and the request is exactly the portable baseline — which is
    # what keeps swapping providers a config-only change.
    tasks: dict[str, dict[str, object]]
    # Applied to every request; a task block overrides it key by key. Where a
    # provider-wide setting lives — `temperature`, say, which is far less
    # portable than it looks: some models accept only their own default.
    extra: dict[str, object]
    # Per-million token rates, for estimating what a run cost. In config
    # because they change, and because the next provider's will differ.
    input_per_million: float
    output_per_million: float

    def extra_for(self, task: str | None) -> dict[str, object]:
        return {**self.extra, **self.tasks.get(task or "", {})}

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_million
            + output_tokens * self.output_per_million
        ) / 1_000_000


@dataclass(frozen=True)
class DiscoverConfig:
    """Stopping conditions. Every one of them is a ceiling, never a target."""

    max_rounds: int
    max_videos: int
    max_requests: int
    quiet_rounds: int
    # How wide each round casts. These multiply: queries × results is the
    # candidate count, and candidates ÷ judge_batch_size is the request count.
    # On a slow provider that product is the difference between a three-minute
    # run and a three-hour one, so both are config rather than constants.
    queries_per_round: int
    results_per_query: int
    # Under per-token billing a request count is a weak guard: one batch of
    # fifty candidates and one batch of five cost the same number of requests
    # and wildly different money. A runaway loop should stop on money.
    max_cost_usd: float
    # Seconds between transcript fetches. YouTube throttles a burst — a run
    # that pulled six videos quickly had the next thirteen fetches all fail in
    # nine seconds. Proper backoff belongs to M3; this keeps a run polite.
    fetch_delay_s: float


@dataclass(frozen=True)
class NoteConfig:
    """The note worker's ceilings. Same shapes as discovery's, different numbers."""

    max_cost_usd: float
    # A transcript longer than this is skipped and logged, never split. The
    # entire premise of a note is one model reading the whole thing at once —
    # chunking would produce something that looks like a note and is not.
    max_transcript_chars: int
    max_tokens: int


@dataclass(frozen=True)
class AskConfig:
    """The answering pass. One call over the whole library, sometimes two."""

    # Generous, because the shape of this call is new: a very large input (every
    # note at once) and a moderate output. `review_brief` spent its entire 8192
    # budget on reasoning and returned nothing, and reasoning bills against this
    # ceiling, so an answer that converges must have room to be written after
    # the thinking is done.
    max_tokens: int
    # Transcripts a single question may open. Escalation is the model's call,
    # and a ceiling on it is the difference between reading three transcripts
    # and reading the library.
    max_transcripts: int
    # Per transcript, when escalating. A 101k-character transcript is a fifth of
    # this budget on its own.
    max_transcript_chars: int


@dataclass(frozen=True)
class Config:
    library_path: Path
    transcript_order: tuple[str, ...]
    search_order: tuple[str, ...]
    youtube_api_key: str | None
    youtube_daily_units: int
    llm: LlmConfig
    discover: DiscoverConfig
    note: NoteConfig
    ask: AskConfig
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
        llm=_llm(data, chosen),
        discover=_discover(data, chosen),
        note=_note(data, chosen),
        ask=_ask(data, chosen),
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


def _llm(data: dict, config_path: Path) -> LlmConfig:
    section = data.get("llm") or {}
    limits = section.get("limits") or {}
    pricing = section.get("pricing") or {}
    raw_key = section.get("api_key")
    if raw_key is not None and not isinstance(raw_key, str):
        raise ConfigError(f"llm.api_key must be a string in {config_path}")
    key = (raw_key or "").strip() or os.environ.get(LLM_API_KEY_ENV, "").strip()

    return LlmConfig(
        provider=_string(section, "provider", "unnamed", config_path, "llm"),
        base_url=_string(section, "base_url", DEFAULT_LLM_BASE_URL, config_path, "llm"),
        api_key=key or None,
        model=_string(section, "model", DEFAULT_LLM_MODEL, config_path, "llm"),
        requests_per_minute=_positive(
            limits, "requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE, config_path, "llm.limits"
        ),
        judge_batch_size=_positive(
            limits, "judge_batch_size", DEFAULT_JUDGE_BATCH_SIZE, config_path, "llm.limits"
        ),
        timeout_s=float(
            _positive(section, "timeout_s", int(DEFAULT_LLM_TIMEOUT_S), config_path, "llm")
        ),
        max_tokens=_positive(section, "max_tokens", DEFAULT_MAX_TOKENS, config_path, "llm"),
        tasks=_tasks(section, config_path),
        extra=_extra(section, "llm", config_path),
        input_per_million=_rate(pricing, "input_per_million", DEFAULT_INPUT_RATE, config_path),
        output_per_million=_rate(pricing, "output_per_million", DEFAULT_OUTPUT_RATE, config_path),
    )


def _tasks(section: dict, config_path: Path) -> dict[str, dict[str, object]]:
    """Read ``[llm.tasks.<name>] extra = { ... }`` without interpreting it.

    The contents are never validated or branched on — that is the point. A
    provider that ignores an unknown field costs nothing; a provider that
    rejects it says so, which is how you find out it was being honoured.
    """
    raw = section.get("tasks") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"llm.tasks must be a table in {config_path}")

    tasks: dict[str, dict[str, object]] = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"llm.tasks.{name} must be a table in {config_path}")
        tasks[str(name)] = _extra(body, f"llm.tasks.{name}", config_path)
    return tasks


def _extra(section: dict, where: str, config_path: Path) -> dict[str, object]:
    extra = section.get("extra", {})
    if not isinstance(extra, dict):
        raise ConfigError(f"{where}.extra must be a table in {config_path}")
    return dict(extra)


def _rate(pricing: dict, key: str, default: float, config_path: Path) -> float:
    raw = pricing.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise ConfigError(f"llm.pricing.{key} must be a non-negative number in {config_path}")
    return float(raw)


def _discover(data: dict, config_path: Path) -> DiscoverConfig:
    section = data.get("discover") or {}
    return DiscoverConfig(
        max_rounds=_positive(section, "max_rounds", DEFAULT_MAX_ROUNDS, config_path, "discover"),
        max_videos=_positive(section, "max_videos", DEFAULT_MAX_VIDEOS, config_path, "discover"),
        max_requests=_positive(
            section, "max_requests", DEFAULT_MAX_REQUESTS, config_path, "discover"
        ),
        quiet_rounds=_positive(
            section, "quiet_rounds", DEFAULT_QUIET_ROUNDS, config_path, "discover"
        ),
        queries_per_round=_positive(
            section, "queries_per_round", DEFAULT_QUERIES_PER_ROUND, config_path, "discover"
        ),
        results_per_query=_positive(
            section, "results_per_query", DEFAULT_RESULTS_PER_QUERY, config_path, "discover"
        ),
        max_cost_usd=float(
            _number(section, "max_cost_usd", DEFAULT_MAX_COST_USD, config_path, "discover")
        ),
        fetch_delay_s=float(
            _number(section, "fetch_delay_s", DEFAULT_FETCH_DELAY_S, config_path, "discover")
        ),
    )


def _note(data: dict, config_path: Path) -> NoteConfig:
    section = data.get("note") or {}
    return NoteConfig(
        max_cost_usd=float(
            _number(section, "max_cost_usd", DEFAULT_NOTE_MAX_COST_USD, config_path, "note")
        ),
        max_transcript_chars=_positive(
            section,
            "max_transcript_chars",
            DEFAULT_NOTE_MAX_TRANSCRIPT_CHARS,
            config_path,
            "note",
        ),
        max_tokens=_positive(section, "max_tokens", DEFAULT_NOTE_MAX_TOKENS, config_path, "note"),
    )


def _ask(data: dict, config_path: Path) -> AskConfig:
    section = data.get("ask") or {}
    return AskConfig(
        max_tokens=_positive(section, "max_tokens", DEFAULT_ASK_MAX_TOKENS, config_path, "ask"),
        max_transcripts=_positive(
            section, "max_transcripts", DEFAULT_ASK_MAX_TRANSCRIPTS, config_path, "ask"
        ),
        max_transcript_chars=_positive(
            section,
            "max_transcript_chars",
            DEFAULT_ASK_MAX_TRANSCRIPT_CHARS,
            config_path,
            "ask",
        ),
    )


def _number(section: dict, key: str, default: float, config_path: Path, where: str) -> float:
    raw = section.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise ConfigError(f"{where}.{key} must be a non-negative number in {config_path}")
    return float(raw)


def _string(section: dict, key: str, default: str, config_path: Path, where: str) -> str:
    raw = section.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string in {config_path}")
    return raw.strip()


def _positive(section: dict, key: str, default: int, config_path: Path, where: str) -> int:
    raw = section.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ConfigError(f"{where}.{key} must be a positive number in {config_path}")
    return int(raw)


def _repo_root() -> Path:
    # src/marginalia/config.py -> repo root
    return Path(__file__).resolve().parents[2]
