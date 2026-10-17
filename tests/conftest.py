"""Shared test scaffolding.

`make_config` exists so that adding a field to `Config` is a one-line change
here rather than an edit to every fixture that builds one. The M2b additions
broke thirty-four tests that had each hand-assembled a Config; that is the
signal for a factory.
"""

from __future__ import annotations

from pathlib import Path

from marginalia.config import AskConfig, Config, DiscoverConfig, LlmConfig, NoteConfig

__all__ = ["make_config", "make_llm_config", "make_discover_config", "make_note_config", "make_ask_config"]


def make_llm_config(**overrides: object) -> LlmConfig:
    values: dict[str, object] = {
        "provider": "test",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
        "model": "test-model",
        "requests_per_minute": 40,
        "judge_batch_size": 10,
        "timeout_s": 30.0,
        "max_tokens": 1024,
        "tasks": {},
        "extra": {},
        "input_per_million": 3.0,
        "output_per_million": 15.0,
    }
    values.update(overrides)
    return LlmConfig(**values)  # type: ignore[arg-type]


def make_discover_config(**overrides: object) -> DiscoverConfig:
    values: dict[str, object] = {
        "max_rounds": 3,
        "max_videos": 25,
        "max_requests": 120,
        "quiet_rounds": 2,
        "queries_per_round": 9,
        "results_per_query": 10,
        "max_cost_usd": 2.0,
        # Zero in tests: pacing is for YouTube's benefit, and a suite that
        # actually slept three seconds per fetch would stop being run.
        "fetch_delay_s": 0.0,
        "trust_caption_hint": False,
    }
    values.update(overrides)
    return DiscoverConfig(**values)  # type: ignore[arg-type]


def make_note_config(**overrides: object) -> NoteConfig:
    values: dict[str, object] = {
        "max_cost_usd": 5.0,
        "max_transcript_chars": 400_000,
        "min_transcript_chars": 1_200,
        "max_tokens": 8192,
        # Off here, on in production. The note tests script one reply per note
        # and describe the writer; the marking pass has its own tests, which
        # turn it on.
        "mark_pass": False,
    }
    values.update(overrides)
    return NoteConfig(**values)  # type: ignore[arg-type]


def make_ask_config(**overrides: object) -> AskConfig:
    values: dict[str, object] = {
        "max_tokens": 32_768,
        "max_transcripts": 3,
        "max_transcript_chars": 120_000,
    }
    values.update(overrides)
    return AskConfig(**values)  # type: ignore[arg-type]


def make_config(tmp_path: Path, **overrides: object) -> Config:
    values: dict[str, object] = {
        "library_path": tmp_path / "library",
        "transcript_order": ("ytdlp",),
        "search_order": ("primary",),
        "youtube_api_key": "key",
        "youtube_daily_units": 10_000,
        "llm": make_llm_config(),
        "discover": make_discover_config(),
        "note": make_note_config(),
        "ask": make_ask_config(),
        "path": tmp_path / "config.toml",
        "created": False,
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]
