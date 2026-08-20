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

# `llm.base_url` and `llm.model` are required and have no default, so almost
# every config in this file needs them even when the test is about something
# else. Appended as its own `[llm]` table where a test does not write one, and
# spelled out inline where it does.
REQUIRED = 'base_url = "https://example.invalid/v1"\nmodel = "a-model"\n'
LLM = '\n[llm]\n' + REQUIRED


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
        BASE.format(path=tmp_path.as_posix())
        + LLM
        + "\n[llm.limits]\nrequests_per_minute = 30\n",
        monkeypatch,
    )
    assert load_config().llm.requests_per_minute == 30


def test_the_key_falls_back_to_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MARGINALIA_LLM_API_KEY", "from-env")
    write(tmp_path, BASE.format(path=tmp_path.as_posix()) + LLM, monkeypatch)
    assert load_config().llm.api_key == "from-env"


def test_the_file_wins_over_the_environment(tmp_path, monkeypatch) -> None:
    """The file is the thing the user edited on purpose."""
    monkeypatch.setenv("MARGINALIA_LLM_API_KEY", "from-env")
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix()) + LLM + 'api_key = "from-file"\n',
        monkeypatch,
    )
    assert load_config().llm.api_key == "from-file"


# -- the two keys with no default -------------------------------------------


@pytest.mark.parametrize("missing", ["base_url", "model"])
def test_a_missing_required_key_is_an_error_not_a_default(
    tmp_path, monkeypatch, missing: str
) -> None:
    """There used to be defaults here, and one of them named the provider this
    project abandoned because it stopped completing requests. A user whose
    config lacked the key got a dead endpoint and a failure three layers from
    the cause."""
    kept = "model" if missing == "base_url" else "base_url"
    value = "a-model" if kept == "model" else "https://example.invalid/v1"
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix()) + f'\n[llm]\n{kept} = "{value}"\n',
        monkeypatch,
    )
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert f"llm.{missing}" in str(caught.value)


def test_the_missing_key_error_names_the_file_and_says_what_to_add(
    tmp_path, monkeypatch
) -> None:
    """An error that only says "required" makes the user go looking. This one
    names the key, the section, the file, and why there is no default."""
    path = write(tmp_path, BASE.format(path=tmp_path.as_posix()), monkeypatch)
    with pytest.raises(ConfigError) as caught:
        load_config()
    message = str(caught.value)

    assert "llm.base_url" in message
    assert str(path) in message
    assert "[llm]" in message
    assert "config.example.toml" in message


def test_no_model_name_survives_in_config_source() -> None:
    """Rule 7 reads literally now: `llm.py` is the only place a model is named.

    `tests/test_boundaries.py` enforces this across the tree; the check is
    repeated here because this is the file that used to be the exception, and
    the exception came back as a default the moment somebody wanted one."""
    import marginalia.config as module

    assert not hasattr(module, "DEFAULT_LLM_MODEL")
    assert not hasattr(module, "DEFAULT_LLM_BASE_URL")


def test_the_created_config_names_the_keys_it_requires(tmp_path, monkeypatch) -> None:
    """First run writes a config file. It must at least tell the user which two
    keys they now have to fill in, or the first real command fails against a
    file that never mentioned them."""
    from marginalia.config import example_text

    body = example_text()
    assert "[llm]" in body
    assert "base_url" in body
    assert "model" in body

def test_the_key_env_var_is_provider_neutral() -> None:
    """Naming it NVIDIA_API_KEY would bake today's vendor into tomorrow's setup."""
    from marginalia.config import LLM_API_KEY_ENV

    assert "NVIDIA" not in LLM_API_KEY_ENV.upper()
    assert LLM_API_KEY_ENV == "MARGINALIA_LLM_API_KEY"


def test_a_missing_key_is_none_not_an_empty_string(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MARGINALIA_LLM_API_KEY", raising=False)
    write(tmp_path, BASE.format(path=tmp_path.as_posix()) + LLM, monkeypatch)
    assert load_config().llm.api_key is None


# -- defaults ---------------------------------------------------------------


def test_only_the_two_required_keys_are_required(tmp_path, monkeypatch) -> None:
    """`base_url` and `model` have no default; everything else still does.

    The point of requiring those two is that no other key became mandatory
    alongside them — a config naming a provider and nothing more must still
    load, or "required" would have quietly meant "fill in the whole file"."""
    monkeypatch.delenv("MARGINALIA_LLM_API_KEY", raising=False)
    write(tmp_path, BASE.format(path=tmp_path.as_posix()) + LLM, monkeypatch)
    config = load_config()

    assert config.llm.requests_per_minute > 0
    assert config.llm.judge_batch_size > 0
    assert config.discover.max_rounds > 0
    assert config.discover.queries_per_round > 0


def test_the_timeout_is_generous_by_default(tmp_path, monkeypatch) -> None:
    """A free tier queues. A request that waits four minutes is still cheaper
    than a run that failed at ninety seconds."""
    write(tmp_path, BASE.format(path=tmp_path.as_posix()) + LLM, monkeypatch)
    assert load_config().llm.timeout_s >= 300


# -- discovery ceilings -----------------------------------------------------


def test_discovery_ceilings_are_read(tmp_path, monkeypatch) -> None:
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix()) + LLM
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
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = 7\n',
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = ""\n',
        '[llm]\n' + REQUIRED + 'api_key = 42\n',
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
        BASE.format(path=tmp_path.as_posix())
        + LLM
        + "\n[llm.limits]\nrequests_per_minute = 0\n",
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
base_url = "https://example.invalid/v1"
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
    write(tmp_path, '[llm]\n' + REQUIRED, monkeypatch)
    config = load_config()

    assert config.llm.tasks == {}
    assert config.llm.extra_for("judge_metadata") == {}
    assert config.llm.extra_for(None) == {}


def test_extra_for_returns_a_copy(tmp_path, monkeypatch) -> None:
    write(tmp_path, """
[llm]
base_url = "https://example.invalid/v1"
model = "m"
[llm.tasks.t]
extra = { a = 1 }
""", monkeypatch)
    config = load_config()
    config.llm.extra_for("t")["a"] = 999

    assert config.llm.extra_for("t") == {"a": 1}


def test_a_malformed_task_block_is_refused(tmp_path, monkeypatch) -> None:
    write(
        tmp_path,
        '[llm]\n' + REQUIRED + '[llm.tasks.t]\nextra = "not a table"\n',
        monkeypatch,
    )
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert "llm.tasks.t.extra" in str(caught.value)


def test_pricing_comes_from_config(tmp_path, monkeypatch) -> None:
    write(tmp_path, """
[llm]
base_url = "https://example.invalid/v1"
model = "m"
[llm.pricing]
input_per_million = 1.5
output_per_million = 9.0
""", monkeypatch)
    config = load_config()

    assert config.llm.cost_usd(1_000_000, 0) == pytest.approx(1.5)
    assert config.llm.cost_usd(0, 1_000_000) == pytest.approx(9.0)


def test_a_negative_rate_is_refused(tmp_path, monkeypatch) -> None:
    write(
        tmp_path,
        '[llm]\n' + REQUIRED + '[llm.pricing]\ninput_per_million = -1\n',
        monkeypatch,
    )
    with pytest.raises(ConfigError):
        load_config()


def test_cost_and_pacing_limits_are_read(tmp_path, monkeypatch) -> None:
    write(
        tmp_path,
        LLM + '[discover]\nmax_cost_usd = 0.5\nfetch_delay_s = 4\n',
        monkeypatch,
    )
    config = load_config()

    assert config.discover.max_cost_usd == 0.5
    assert config.discover.fetch_delay_s == 4.0


def test_the_caption_hint_is_distrusted_by_default(tmp_path, monkeypatch) -> None:
    """Measured, not cautious. `contentDetails.caption` counts only manually
    uploaded tracks: against this project's library it called 31 of the 37
    working videos captionless, getting all 6 manual ones right and all 31
    auto-generated ones wrong. Believing it would route most of a discovery run
    into a queue whose worker does not exist until M4."""
    write(tmp_path, BASE.format(path=tmp_path.as_posix()) + LLM, monkeypatch)
    assert load_config().discover.trust_caption_hint is False


def test_the_caption_hint_can_be_switched_on(tmp_path, monkeypatch) -> None:
    """One line, for when ASR lands or a provider starts counting
    auto-captions."""
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix())
        + LLM
        + "\n[discover]\ntrust_caption_hint = true\n",
        monkeypatch,
    )
    assert load_config().discover.trust_caption_hint is True


def test_a_non_boolean_caption_hint_is_refused(tmp_path, monkeypatch) -> None:
    """"true" is not true. Silently coercing it is how `bool("false")` becomes
    True, which is the exact mistake this whole field exists around."""
    write(
        tmp_path,
        BASE.format(path=tmp_path.as_posix())
        + LLM
        + '\n[discover]\ntrust_caption_hint = "yes"\n',
        monkeypatch,
    )
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert "discover.trust_caption_hint" in str(caught.value)
