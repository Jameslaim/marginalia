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
    "library_root",
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
# variable after one provider would bake today's choice into tomorrow's setup.
LLM_API_KEY_ENV = "MARGINALIA_LLM_API_KEY"

# `base_url` and `model` deliberately have no default. There was one, and it
# named the provider this project abandoned because it stopped completing
# requests — so a user whose config lacked the key got a dead endpoint and a
# failure that pointed nowhere near the cause. There is also no such thing as a
# provider-neutral model name: any default here is a vendor choice made on the
# user's behalf, silently. Both keys are now required, and rule 7 — `llm.py` is
# the only place a model is named — is literally true.
DEFAULT_REQUESTS_PER_MINUTE = 40
DEFAULT_JUDGE_BATCH_SIZE = 10
# Generous on purpose: a hosted free tier can queue, and a request that waits
# four minutes is still cheaper than a failed run.
DEFAULT_LLM_TIMEOUT_S = 420.0
DEFAULT_MAX_TOKENS = 4096

DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_VIDEOS = 25
DEFAULT_MAX_REQUESTS = 120
DEFAULT_QUIET_ROUNDS = 2
DEFAULT_QUERIES_PER_ROUND = 9
DEFAULT_RESULTS_PER_QUERY = 10
# B4. Queries the territory map is asked for, on top of the brief's own
# expansion, and they run once — in round one. Both sets run, deliberately: the
# brief's queries are what discovery did before the map existed, and keeping
# them lets `queries.origin` answer "did the map find anything the old
# expansion would not have?" from the ledger rather than from an opinion.
#
# It is a real cost. Each query is a search, and on the official API a search
# is 100 quota units out of 10,000 a day, so this number and
# `queries_per_round` add rather than replace.
DEFAULT_TERRITORY_QUERIES = 8
# B5. Queries the round planner is asked for, at the end of every round but the
# last. They go *first* in the next round — they were written against the gaps
# that round actually left open, while expansion is still working from the brief
# alone — and the expansion still runs alongside them, so `queries.origin`
# answers "did the planner find anything expansion would not have?" from the
# ledger rather than from an opinion.
DEFAULT_PLANNER_QUERIES = 6

# Output ceilings for the two long structured calls in discovery. Both were
# sharing the global `[llm] max_tokens = 8192` and both are the shape that
# overruns it: a territory map is two term lists plus a dozen query blocks, and
# a plan is a coverage assessment plus queries plus revisions.
#
# Measured rather than guessed. The mapping call on `AI agent memory` hit 8,192
# and returned a fragment, which the truncation guard correctly discarded —
# costing that run its entire territory feature. The planning call on the same
# run reached 5,607 output tokens, 68% of the same ceiling, and the cumulative
# kept set now makes plans longer still.
#
# Raising a ceiling costs nothing when it is not reached: this is per-token
# billing and unused headroom is unused. What it buys is that the guard fires on
# a genuine runaway rather than on an ordinary broad topic.
# The brief and its review. Fourth task on this provider to overrun the global
# ceiling, and the quietest failure of the four: a truncated review falls back
# to the unreviewed draft with one warning line, and every verdict in that run
# is then measured against a brief whose gaps were never closed.
#
# MEASURED on `AI agent memory`, the topic that failed:
#
#   ceiling   call      finish   output   result
#   8192      review    length    8,192   EMPTY — 0 chars back, draft kept
#   16384     draft     stop      1,556   valid
#   16384     review    stop      1,703   valid, 3,343-char brief, review used
#
# Total 3,259 output tokens, 35.7s, $0.0011 for both calls.
#
# **The review converges at ~10% of the new ceiling and still spent all of the
# old one.** That is the whole shape of this failure: the variance is in the
# reasoning, which bills against `max_tokens`, not in the length of the answer.
# A ceiling sized to the output is therefore sized to the wrong thing — headroom
# has to cover a reasoning excursion, and unused headroom costs nothing under
# per-token billing.
#
# **Then the draft hit it.** Six drafts with the current `write_brief` prompt
# used 7,725–14,007 output tokens when they finished, and one reasoned to the
# 16,384 ceiling and returned 0 characters; across two sessions, 2 of 12. The
# earlier, shorter prompts finished 18 of 18 at up to 14,469. The longer the
# prompt, the longer the reasoning, so the ceiling is doubled to about twice
# the largest finished draft, and `write_brief` retries one cut draft rather
# than reviewing an empty one (see `Judge.write_brief`).
DEFAULT_BRIEF_MAX_TOKENS = 32768
# The library-coverage call. One line per brief item, so the output is small —
# but it is the fifth long-structured-output task on this provider and the
# reasoning is what overruns, not the answer. **Then the evidence grew.** Shown
# each note's judge reason and claims (about 17k tokens for 37 notes, against
# 2k of index lines) and asked to itemise each part, one replay reasoned to the
# 16,384 ceiling and returned 613 characters; at a higher ceiling the same call
# finished at 18,939. Doubled, as the brief's was, to about 1.7 times that.
DEFAULT_BASELINE_MAX_TOKENS = 32768
# How many baseline calls a fresh baseline makes, resolved toward missing
# (`brain/coverage.py`). Measured on two topics, three calls each on unchanged
# notes: 1 of 14 and 1 of 11 items changed state, and one call named 3 to 6 of
# the 7 gaps any call named on one topic; any two named 5 to 7, and caught both
# items' less-held state. A third call only when two disagree would have run on
# 5 of 6 pairs. A reused baseline makes none. 1 is the old behaviour.
DEFAULT_BASELINE_CALLS = 2
# A gap is exhausted when at least this many queries aimed at it, over at least
# two rounds in a row, brought no stage-2 keeper on it: it gets no more queries,
# and when every open gap is exhausted the run stops (`gaps_exhausted`).
# Measured on two real runs: every gap that ever brought a keeper did so within
# its first two rounds, and the longest stretch of queries a gap later broke
# with a keeper was 5. The two that never did had 10 and 5 queries over two
# rounds, and 16 and 6 over three. 6 is above the 5 and stops the first at the
# end of round two.
DEFAULT_GAP_EXHAUSTED_AFTER = 6
# Whether the last round may release candidates on held items past the held
# cap to fill the video budget. Off: measured, the release bought 9 and 14
# held candidates in two runs' last rounds, and 13 and 8 of their stored
# videos, nearly all repeats of parts the library already held.
DEFAULT_RELEASE_HELD_LAST_ROUND = False
DEFAULT_TERRITORY_MAX_TOKENS = 16384
DEFAULT_PLANNER_MAX_TOKENS = 16384
# Whether to believe `contentDetails.caption` when it says a video has no
# captions, and skip the fetch.
#
# **Off, because it was measured.** Against the 40 videos in this library whose
# caption outcome is already known, the API reported `caption: "false"` for 31
# of the 37 that yielded a perfectly good transcript. It is not a noisy signal —
# it is an exact one answering a different question: it reports *manually
# uploaded* caption tracks, and got all 6 manual ones right and all 31
# auto-generated ones wrong. yt-dlp reads auto-captions; the API does not count
# them.
#
# Trusting it would therefore route 84% of what discovery finds into
# `needs_asr`, a queue whose worker is M4 and does not exist yet. That is not a
# wasted request saved; it is the library.
#
# The mechanism is built and tested, so this is one line to flip if a future
# provider reports auto-captions honestly, or if ASR lands and being wrong stops
# being terminal.
DEFAULT_TRUST_CAPTION_HINT = False
# Which source writes round one's planned queries: the search plan, written from
# the brief's coverage items (`plan/search`), or the territory map
# (`territory/map`). `plan` since a live run compared the two (rule 39).
# Measured on a live, same-day comparison of
# round one: on a topic whose parts were all held, the plan's 8 queries brought 9
# keepers on missing items and the map's brought 0 (its 12 keepers were general
# explainers of held parts); on a topic whose missing items barely appear on the
# video site, neither found any.
# `map` stays reachable by the setting and by `--search-source map`, and a
# failed plan falls back to the map, never to nothing.
DEFAULT_SEARCH_SOURCE = "plan"
SEARCH_SOURCE_VALUES = ("plan", "map")
# Step 4: a few web searches at the start of a run, digested into the plan's
# CURRENT CONTEXT. Off until a live comparison has measured it. Six searches:
# one per gap for the gaps a baseline typically leaves (4 to 7 measured) plus
# the one general query, at $0.0045 a run; more mostly re-find the same pages.
# Ten results each, because the price is per search, not per result, and the
# digest reads summaries only.
DEFAULT_WEB_CONTEXT = False
DEFAULT_WEB_CONTEXT_QUERIES = 6
DEFAULT_WEB_CONTEXT_RESULTS = 10
DEFAULT_MAX_COST_USD = 2.0
DEFAULT_FETCH_DELAY_S = 3.0

# A note pass reads whole transcripts, so its ceiling is a different number
# from discovery's even though it is the same kind of guard.
DEFAULT_NOTE_MAX_COST_USD = 5.0
# Characters, not tokens: counting tokens needs a tokenizer, and a tokenizer is
# a provider-shaped dependency.
#
# MEASURED, after 400,000 turned out to be stale. Five transcripts in the
# library exceeded it — four of them videos a run had judged YES and counted as
# coverage, while `/ask` could never reach them because they would never be
# noted. The longest, `yodh-oEFnb4` at 768,878 characters (~190k tokens, a
# 7.6-hour course), notes in **2m05s for $0.04** and returns a valid v5 note.
# The ceiling was costing far more than it saved.
#
# A million characters is roughly 250k tokens: ~30% headroom over the longest
# transcript ever seen here, and about 5¢. The *reason* for a ceiling at all is
# unchanged — one note is one model reading one thing whole, and past some
# length that stops being true — but the number was guessed and is now measured.
DEFAULT_NOTE_MAX_TRANSCRIPT_CHARS = 1_000_000
# The other end. 1,200 because the note/transcript ratio breaks there: the
# library median is 0.469x and the p90 is 1.42x, while 1,153 characters
# produced 7.4x and 37 characters produced 126x. Low on purpose — a floor
# at 2,000 would start excluding genuine short-form video, which is the
# duration bias the stage-1 prompt forbids, moved somewhere less visible.
DEFAULT_NOTE_MIN_TRANSCRIPT_CHARS = 1_200
# Higher than the global default: the reply is a full note, and on a provider
# whose thinking is always on the reasoning bills against the same ceiling.
#
# 32768 rather than 8192, and for the reason the shipped config has carried
# since 554fda6 and this default did not: at 8192 the reasoning and the note
# share a budget too small for both, a truncated reply loses a required
# section, `validate_note` fails it, and three failures in a row stop the run.
# The measurement lived in config.example.toml alone, so a machine that never
# wrote the key got the pre-measurement number, and nothing said so.
DEFAULT_NOTE_MAX_TOKENS = 32_768
# The second pass that marks what a note's writer left unmarked. On by default:
# one note audited under write_note v9 had 14 unmarked statements of outside
# knowledge or arithmetic, and ask reads a note as what the video says.
DEFAULT_NOTE_MARK_PASS = True

# The answering pass. Every note at once is a large input and a moderate output,
# a shape this provider has not been measured on, so the ceiling is generous
# and the effort setting is verified rather than assumed.
DEFAULT_ASK_MAX_TOKENS = 32_768
DEFAULT_ASK_MAX_TRANSCRIPTS = 3
DEFAULT_ASK_MAX_TRANSCRIPT_CHARS = 120_000
# M7. What fraction of the input ceiling selected notes may fill. A policy
# rather than a provider fact, which is why this one has a default where
# `max_input_tokens` deliberately does not.
DEFAULT_SELECT_BUDGET_SHARE = 0.75

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

# Required — both, with no default. Fill these in before running anything that
# calls a model. See config.example.toml for a worked example.
[llm]
# base_url = "https://..."
# model = "..."
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
    # Skip the fetch when the search provider reports no caption track. Off by
    # default and measured; see DEFAULT_TRUST_CAPTION_HINT.
    trust_caption_hint: bool = DEFAULT_TRUST_CAPTION_HINT
    # B4. How many queries the territory map is asked to write. Added to
    # `queries_per_round` in round one, never substituted for it.
    territory_queries: int = DEFAULT_TERRITORY_QUERIES
    # B5. How many the planner is asked for at the end of each round. Added to
    # `queries_per_round` from round two, never substituted for it.
    planner_queries: int = DEFAULT_PLANNER_QUERIES
    # Output ceilings for the mapping and planning calls. Separate from the
    # global one because these two are the long structured outputs, and a
    # truncated reply from either is discarded whole rather than parsed.
    brief_max_tokens: int = DEFAULT_BRIEF_MAX_TOKENS
    # Refuse to run a topic with no approved brief rather than write a fresh
    # one. Off by default, so an unattended run never blocks on a person.
    require_approved_brief: bool = False
    # `plan` or `map`: what writes round one's planned queries. See
    # DEFAULT_SEARCH_SOURCE.
    search_source: str = DEFAULT_SEARCH_SOURCE
    # Step 4's lookup, and its two ceilings. See DEFAULT_WEB_CONTEXT.
    web_context: bool = DEFAULT_WEB_CONTEXT
    web_context_queries: int = DEFAULT_WEB_CONTEXT_QUERIES
    web_context_results: int = DEFAULT_WEB_CONTEXT_RESULTS
    baseline_max_tokens: int = DEFAULT_BASELINE_MAX_TOKENS
    baseline_calls: int = DEFAULT_BASELINE_CALLS
    # See DEFAULT_GAP_EXHAUSTED_AFTER and DEFAULT_RELEASE_HELD_LAST_ROUND.
    gap_exhausted_after: int = DEFAULT_GAP_EXHAUSTED_AFTER
    release_held_last_round: bool = DEFAULT_RELEASE_HELD_LAST_ROUND
    territory_max_tokens: int = DEFAULT_TERRITORY_MAX_TOKENS
    planner_max_tokens: int = DEFAULT_PLANNER_MAX_TOKENS
    # Stage 2's own output ceiling. `None` — the default — keeps the global
    # `[llm] max_tokens`, which is what stage 2 has always had. It exists for
    # reasoning effort: at no reasoning a stage-2 reply is about 150 tokens, and
    # at `low` 241 replayed calls had a median of 1,643 and a maximum of 8,192 —
    # one reasoned to the global ceiling and returned nothing. A setting rather
    # than a default, because the effort that needs it is a setting too.
    judge_full_max_tokens: int | None = None


@dataclass(frozen=True)
class NoteConfig:
    """The note worker's ceilings. Same shapes as discovery's, different numbers."""

    max_cost_usd: float
    # A transcript longer than this is skipped and logged, never split. The
    # entire premise of a note is one model reading the whole thing at once —
    # chunking would produce something that looks like a note and is not.
    max_transcript_chars: int
    # M-later. Below this, there is not enough transcript for the note's sections
    # to be written from. Measured: a note is normally ~0.47x its transcript,
    # and below ~1,200 characters that inverts to 5x-126x.
    min_transcript_chars: int
    max_tokens: int
    # A second, small call per note that lists the writer's unmarked outside
    # statements; the code wraps them in markers. See `enrich/note.py`.
    mark_pass: bool = DEFAULT_NOTE_MARK_PASS


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
    # The provider's context window, for the headroom guard. **No default, and
    # that is rule 7's argument applied one field over**: a context limit is
    # provider-specific, so any number here would be a vendor's figure chosen on
    # the user's behalf and silently wrong on the next provider. Absent, `ask`
    # reports the size it is about to send and nothing else. Present, it warns
    # above 80% and refuses above 100%.
    max_input_tokens: int | None = None
    # M7. The share of `max_input_tokens` note selection may fill. Not
    # provider-specific — a policy, not a limit — so unlike the ceiling it has a
    # default. Below this the whole library loads; above it the selector runs
    # and fills back up to here.
    #
    # 0.75 leaves room for the question, the prompt, the escalation block and a
    # transcript the model may open mid-answer, none of which the note estimate
    # covers. Raising it buys context and narrows that margin.
    select_budget_share: float = DEFAULT_SELECT_BUDGET_SHARE


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


def library_root() -> Path:
    """The library path alone, validating nothing else.

    `marg guard-write` needs this and only this. Routing it through
    `load_config()` would make the write guard depend on the `[llm]` section
    being complete — and because the guard deliberately fails *open*, an
    incomplete `[llm]` block would silently switch off hard rule 2. A guard that
    stops guarding when an unrelated key is missing is worse than no guard,
    because it still looks present.

    Found by the hook tests the moment `base_url` and `model` became required.
    """
    chosen = next((path for path in _candidates() if path.is_file()), None)
    if chosen is None:
        raise ConfigError("no config file found; run any other `marg` command to create one")
    try:
        data = tomllib.loads(chosen.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config at {chosen}: {exc}") from exc
    return _library_path(data, chosen)


def _candidates() -> list[Path]:
    override = os.environ.get("MARGINALIA_CONFIG")
    if override:
        return [Path(override).expanduser()]
    return [Path.cwd() / "config.toml", Path.home() / ".marginalia" / "config.toml"]


def _library_path(data: dict, config_path: Path) -> Path:
    raw = (data.get("library") or {}).get("path")
    if raw is None:
        raw = DEFAULT_LIBRARY_PATH
    # An empty string used to fall through `or` to the default — a library the
    # user had just tried to point somewhere else.
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"library.path must be a non-empty string in {config_path}")
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
        base_url=_required_string(section, "base_url", config_path, "llm"),
        api_key=key or None,
        model=_required_string(section, "model", config_path, "llm"),
        requests_per_minute=_positive(
            limits, "requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE, config_path, "llm.limits"
        ),
        judge_batch_size=_positive(
            limits, "judge_batch_size", DEFAULT_JUDGE_BATCH_SIZE, config_path, "llm.limits"
        ),
        timeout_s=_positive_number(
            section, "timeout_s", DEFAULT_LLM_TIMEOUT_S, config_path, "llm"
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
        trust_caption_hint=_flag(
            section, "trust_caption_hint", DEFAULT_TRUST_CAPTION_HINT, config_path, "discover"
        ),
        territory_queries=_positive(
            section, "territory_queries", DEFAULT_TERRITORY_QUERIES, config_path, "discover"
        ),
        planner_queries=_positive(
            section, "planner_queries", DEFAULT_PLANNER_QUERIES, config_path, "discover"
        ),
        brief_max_tokens=_positive(
            section, "brief_max_tokens", DEFAULT_BRIEF_MAX_TOKENS,
            config_path, "discover",
        ),
        require_approved_brief=_flag(
            section, "require_approved_brief", False, config_path, "discover"
        ),
        search_source=_choice(
            section, "search_source", DEFAULT_SEARCH_SOURCE, SEARCH_SOURCE_VALUES,
            config_path, "discover",
        ),
        web_context=_flag(section, "web_context", DEFAULT_WEB_CONTEXT, config_path, "discover"),
        web_context_queries=_positive(
            section, "web_context_queries", DEFAULT_WEB_CONTEXT_QUERIES, config_path, "discover"
        ),
        web_context_results=_positive(
            section, "web_context_results", DEFAULT_WEB_CONTEXT_RESULTS, config_path, "discover"
        ),
        baseline_max_tokens=_positive(
            section, "baseline_max_tokens", DEFAULT_BASELINE_MAX_TOKENS,
            config_path, "discover",
        ),
        baseline_calls=_positive(
            section, "baseline_calls", DEFAULT_BASELINE_CALLS, config_path, "discover"
        ),
        gap_exhausted_after=_positive(
            section, "gap_exhausted_after", DEFAULT_GAP_EXHAUSTED_AFTER, config_path, "discover"
        ),
        release_held_last_round=_flag(
            section, "release_held_last_round", DEFAULT_RELEASE_HELD_LAST_ROUND,
            config_path, "discover",
        ),
        territory_max_tokens=_positive(
            section, "territory_max_tokens", DEFAULT_TERRITORY_MAX_TOKENS,
            config_path, "discover",
        ),
        planner_max_tokens=_positive(
            section, "planner_max_tokens", DEFAULT_PLANNER_MAX_TOKENS,
            config_path, "discover",
        ),
        judge_full_max_tokens=(
            _positive(section, "judge_full_max_tokens", 1, config_path, "discover")
            if section.get("judge_full_max_tokens") is not None
            else None
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
        min_transcript_chars=_positive(
            section,
            "min_transcript_chars",
            DEFAULT_NOTE_MIN_TRANSCRIPT_CHARS,
            config_path,
            "note",
        ),
        max_tokens=_positive(section, "max_tokens", DEFAULT_NOTE_MAX_TOKENS, config_path, "note"),
        mark_pass=_flag(section, "mark_pass", DEFAULT_NOTE_MARK_PASS, config_path, "note"),
    )


def _flag(section: dict, key: str, default: bool, config_path: Path, name: str) -> bool:
    raw = section.get(key)
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ConfigError(f"{name}.{key} must be true or false in {config_path}")
    return raw


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
        select_budget_share=_share(
            section, "select_budget_share", DEFAULT_SELECT_BUDGET_SHARE, config_path, "ask"
        ),
        # No default: absent means "do not guess", not "use this number".
        max_input_tokens=(
            _positive(section, "max_input_tokens", 0, config_path, "ask")
            if section.get("max_input_tokens") is not None
            else None
        ),
    )


def _number(section: dict, key: str, default: float, config_path: Path, where: str) -> float:
    raw = section.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise ConfigError(f"{where}.{key} must be a non-negative number in {config_path}")
    return float(raw)


def _required_string(section: dict, key: str, config_path: Path, where: str) -> str:
    """A key with no default: absent is an error, and the error says what to do.

    The message names the key, the section it belongs in, and the file — because
    the alternative failure is a request to a provider the user never chose,
    surfacing three layers away as a connection error or an unknown-model 404.
    """
    raw = section.get(key)
    if raw is None:
        raise ConfigError(
            f"{where}.{key} is required and missing from {config_path}\n"
            f"  add it under [{where}]:\n"
            f'      {key} = "..."\n'
            f"  {where}.base_url and {where}.model have no default: there is no "
            f"provider-neutral value for either, and guessing one means calling a "
            f"model you did not choose. See config.example.toml."
        )
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string in {config_path}")
    return raw.strip()


def _flag(section: dict, key: str, default: bool, config_path: Path, where: str) -> bool:
    raw = section.get(key)
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ConfigError(f"{where}.{key} must be true or false in {config_path}")
    return raw


def _string(section: dict, key: str, default: str, config_path: Path, where: str) -> str:
    raw = section.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string in {config_path}")
    return raw.strip()


def _choice(
    section: dict, key: str, default: str, values: tuple[str, ...], config_path: Path, where: str
) -> str:
    value = _string(section, key, default, config_path, where).lower()
    if value not in values:
        raise ConfigError(
            f"{where}.{key} must be one of {', '.join(values)} in {config_path}, not {value!r}"
        )
    return value


def _positive(section: dict, key: str, default: int, config_path: Path, where: str) -> int:
    """A positive whole number. A fraction is refused, not truncated: `int()`
    turned `max_videos = 0.4` into 0 and `judge_batch_size = 0.9` into a batch
    size that raises mid-run, after quota had been spent."""
    raw = section.get(key)
    if raw is None:
        return default
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or raw <= 0
        or (isinstance(raw, float) and not raw.is_integer())
    ):
        raise ConfigError(f"{where}.{key} must be a positive whole number in {config_path}")
    return int(raw)


def _positive_number(
    section: dict, key: str, default: float, config_path: Path, where: str
) -> float:
    """A positive number that may be fractional — a timeout. Read through
    `_positive` it was truncated, and `timeout_s = 0.5` became 0: every request
    timed out at once."""
    raw = section.get(key)
    if raw is None:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ConfigError(f"{where}.{key} must be a positive number in {config_path}")
    return float(raw)


def _share(section: dict, key: str, default: float, config_path: Path, where: str) -> float:
    """A fraction of a whole, above 0 and at most 1."""
    raw = section.get(key)
    if raw is None:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw <= 1:
        raise ConfigError(f"{where}.{key} must be above 0 and at most 1 in {config_path}")
    return float(raw)


def _repo_root() -> Path:
    # src/marginalia/config.py -> repo root
    return Path(__file__).resolve().parents[2]
