"""Configuration, and especially the parts a provider swap touches.

The claim M2b makes is that changing model provider is three values in a file
and no code. That claim lives or dies here: if any provider-shaped thing is a
constant rather than a config key, the swap becomes a code change and the
abstraction has already failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marginalia.config import ConfigError, load_config


def write(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("MARGINALIA_CONFIG", str(path))
    return path


BASE = '[library]\npath = "{path}"\n'


# -- the swap surface -------------------------------------------------------


def test_the_three_swap_values_come_from_the_file(tmp_path, monkeypatch) -> None:
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix())
        + """
[llm]
provider = "groq"
base_url = "https://api.groq.com/openai/v1"
model = "some/model"
api_key = "gsk_example"
""",
        monkeypatch,
    )
    llm = load_config().llm

    assert llm.provider == "groq"
    assert llm.base_url == "https://api.groq.com/openai/v1"
    assert llm.model == "some/model"
    assert llm.api_key == "gsk_example"


def test_the_rate_ceiling_is_config_not_code(tmp_path, monkeypatch) -> None:
    """The next provider will have a different limit. If this were a constant,
    swapping would mean editing Python."""
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix()) + "\n[llm.limits]\nrequests_per_minute = 30\n",
        monkeypatch,
    )
    assert load_config().llm.requests_per_minute == 30


def test_the_key_falls_back_to_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MARGINALIA_LLM_API_KEY", "from-env")
    write(tmp_path, BASE.format(path=tmp_path.as_posix()), monkeypatch)
    assert load_config().llm.api_key == "from-env"


def test_the_file_wins_over_the_environment(tmp_path, monkeypatch) -> None:
    """The file is the thing the user edited on purpose."""
    monkeypatch.setenv("MARGINALIA_LLM_API_KEY", "from-env")
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix()) + '\n[llm]\napi_key = "from-file"\n',
        monkeypatch,
    )
    assert load_config().llm.api_key == "from-file"


def test_the_key_env_var_is_provider_neutral() -> None:
    """Naming it NVIDIA_API_KEY would bake today's vendor into tomorrow's setup."""
    from marginalia.config import LLM_API_KEY_ENV

    assert "NVIDIA" not in LLM_API_KEY_ENV.upper()
    assert LLM_API_KEY_ENV == "MARGINALIA_LLM_API_KEY"


def test_a_missing_key_is_none_not_an_empty_string(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MARGINALIA_LLM_API_KEY", raising=False)
    write(tmp_path, BASE.format(path=tmp_path.as_posix()), monkeypatch)
    assert load_config().llm.api_key is None


# -- defaults ---------------------------------------------------------------


def test_an_empty_config_still_yields_a_usable_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MARGINALIA_LLM_API_KEY", raising=False)
    write(tmp_path, BASE.format(path=tmp_path.as_posix()), monkeypatch)
    config = load_config()

    assert config.llm.requests_per_minute > 0
    assert config.llm.judge_batch_size > 0
    assert config.discover.max_rounds > 0
    assert config.discover.queries_per_round > 0


def test_the_timeout_is_generous_by_default(tmp_path, monkeypatch) -> None:
    """A free tier queues. A request that waits four minutes is still cheaper
    than a run that failed at ninety seconds."""
    write(tmp_path, BASE.format(path=tmp_path.as_posix()), monkeypatch)
    assert load_config().llm.timeout_s >= 300


# -- discovery ceilings -----------------------------------------------------


def test_discovery_ceilings_are_read(tmp_path, monkeypatch) -> None:
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix())
        + """
[discover]
max_rounds = 5
max_videos = 40
max_requests = 200
quiet_rounds = 3
queries_per_round = 6
results_per_query = 12
""",
        monkeypatch,
    )
    discover = load_config().discover

    assert (discover.max_rounds, discover.max_videos) == (5, 40)
    assert (discover.max_requests, discover.quiet_rounds) == (200, 3)
    assert (discover.queries_per_round, discover.results_per_query) == (6, 12)


# -- errors surface ---------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '[llm]\nmodel = 7\n',
        '[llm]\nmodel = ""\n',
        '[llm]\napi_key = 42\n',
        '[llm.limits]\nrequests_per_minute = 0\n',
        '[llm.limits]\njudge_batch_size = -1\n',
        '[discover]\nmax_rounds = 0\n',
        '[discover]\nqueries_per_round = "lots"\n',
    ],
)
def test_a_malformed_value_is_refused_not_defaulted(tmp_path, monkeypatch, body: str) -> None:
    """Silently falling back to a default would hide a typo in the one file
    that decides what the run costs."""
    write(tmp_path, BASE.format(path=tmp_path.as_posix()) + "\n" + body, monkeypatch)
    with pytest.raises(ConfigError):
        load_config()


def test_the_error_names_the_key_and_the_file(tmp_path, monkeypatch) -> None:
    path = write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix()) + "\n[llm.limits]\nrequests_per_minute = 0\n",
        monkeypatch,
    )
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert "llm.limits.requests_per_minute" in str(caught.value)
    assert str(path) in str(caught.value)


def test_the_shipped_example_parses(tmp_path, monkeypatch) -> None:
    """config.example.toml is what a new install starts from. If it does not
    load, first run fails for everyone."""
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    write(tmp_path, example.read_text(encoding="utf-8"), monkeypatch)

    config = load_config()
    assert config.llm.model
    assert config.llm.base_url.startswith("http")
    assert config.discover.max_rounds > 0


# -- per-task request extras and pricing (M2b) -----------------------------


def test_task_blocks_are_read_without_being_interpreted(tmp_path, monkeypatch) -> None:
    """config.py stores whatever is in `extra` and never looks inside it."""
    write(tmp_path, """
[llm]
model = "m"

[llm.tasks.judge_metadata]
extra = { reasoning_effort = "low" }

[llm.tasks.write_brief]
extra = { reasoning_effort = "high", something_unknown = 7 }
""", monkeypatch)
    config = load_config()

    assert config.llm.tasks["judge_metadata"] == {"reasoning_effort": "low"}
    assert config.llm.tasks["write_brief"] == {
        "reasoning_effort": "high",
        "something_unknown": 7,
    }


def test_a_provider_without_task_blocks_gets_empty_extras(tmp_path, monkeypatch) -> None:
    """The config-only swap depends on this: no blocks means every request is
    the portable baseline."""
    write(tmp_path, '[llm]\nmodel = "m"\n', monkeypatch)
    config = load_config()

    assert config.llm.tasks == {}
    assert config.llm.extra_for("judge_metadata") == {}
    assert config.llm.extra_for(None) == {}


def test_extra_for_returns_a_copy(tmp_path, monkeypatch) -> None:
    write(tmp_path, """
[llm]
model = "m"
[llm.tasks.t]
extra = { a = 1 }
""", monkeypatch)
    config = load_config()
    config.llm.extra_for("t")["a"] = 999

    assert config.llm.extra_for("t") == {"a": 1}


def test_a_malformed_task_block_is_refused(tmp_path, monkeypatch) -> None:
    write(tmp_path, '[llm]\nmodel = "m"\n[llm.tasks.t]\nextra = "not a table"\n', monkeypatch)
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert "llm.tasks.t.extra" in str(caught.value)


def test_pricing_comes_from_config(tmp_path, monkeypatch) -> None:
    write(tmp_path, """
[llm]
model = "m"
[llm.pricing]
input_per_million = 1.5
output_per_million = 9.0
""", monkeypatch)
    config = load_config()

    assert config.llm.cost_usd(1_000_000, 0) == pytest.approx(1.5)
    assert config.llm.cost_usd(0, 1_000_000) == pytest.approx(9.0)


def test_a_negative_rate_is_refused(tmp_path, monkeypatch) -> None:
    write(tmp_path, '[llm]\nmodel = "m"\n[llm.pricing]\ninput_per_million = -1\n', monkeypatch)
    with pytest.raises(ConfigError):
        load_config()


def test_cost_and_pacing_limits_are_read(tmp_path, monkeypatch) -> None:
    write(tmp_path, '[discover]\nmax_cost_usd = 0.5\nfetch_delay_s = 4\n', monkeypatch)
    config = load_config()

    assert config.discover.max_cost_usd == 0.5
    assert config.discover.fetch_delay_s == 4.0
