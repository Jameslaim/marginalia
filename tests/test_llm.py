"""The model client: pacing, retries, accounting, prompts.

No network. The clock and sleep are injected, so the limiter's behaviour is
asserted in microseconds rather than by actually waiting a minute.

Two things here are load-bearing for the whole milestone. The limiter must
*block* rather than raise, because a discovery run legitimately needs more
requests than the per-minute ceiling allows. And the retry classifier must not
retry a config error, because burning four attempts and several minutes on a
bad API key helps nobody.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_llm_config

from marginalia.llm import (
    LlmClient,
    LlmConfigError,
    LlmError,
    LlmUnavailable,
    RateLimiter,
    Usage,
    batched,
    load_prompt,
    prompts_dir,
)


class FakeClock:
    """Monotonic time under test control. Sleeping advances it, as it would."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeResponse:
    def __init__(self, text: str, model: str = "test-model", tokens: tuple[int, int] = (10, 5)):
        self.model = model
        self.usage = FakeUsage(*tokens)
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message})()]


class HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def client(*responses, **kwargs) -> tuple[LlmClient, list[dict]]:
    """A client whose transport replays `responses`; exceptions are raised."""
    calls: list[dict] = []
    queue = list(responses)

    def transport(**request):
        calls.append(request)
        item = queue.pop(0) if queue else FakeResponse("ok")
        if isinstance(item, Exception):
            raise item
        return item

    clock = kwargs.pop("clock", None) or FakeClock()
    limiter = RateLimiter(kwargs.pop("rpm", 40), now=clock.now, sleep=clock.sleep)
    instance = LlmClient(
        make_llm_config(**kwargs.pop("config", {})),
        transport=transport,
        limiter=limiter,
        sleep=clock.sleep,
        **kwargs,
    )
    return instance, calls


# -- the rate limiter -------------------------------------------------------


def test_the_limiter_allows_a_full_bucket_without_waiting() -> None:
    clock = FakeClock()
    limiter = RateLimiter(40, now=clock.now, sleep=clock.sleep)

    for _ in range(40):
        limiter.acquire()

    assert clock.slept == []
    assert limiter.waits == 0


def test_the_limiter_blocks_rather_than_raising_under_a_burst() -> None:
    """Failing at request 41 would be the wrong shape entirely. A discovery
    run is supposed to exceed the ceiling — just not all at once."""
    clock = FakeClock()
    limiter = RateLimiter(40, now=clock.now, sleep=clock.sleep)

    for _ in range(60):
        limiter.acquire()  # must not raise

    assert limiter.waits == 20, "the 20 requests past the bucket should each have paced"
    assert all(delay > 0 for delay in clock.slept)


def test_the_limiter_paces_to_the_configured_rate() -> None:
    clock = FakeClock()
    limiter = RateLimiter(60, now=clock.now, sleep=clock.sleep)  # one per second

    for _ in range(60):
        limiter.acquire()
    start = clock.t
    for _ in range(10):
        limiter.acquire()

    # Ten requests past a drained bucket at 1/s costs about ten seconds.
    assert 9.0 <= clock.t - start <= 11.0


def test_the_limiter_refills_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(60, now=clock.now, sleep=clock.sleep)

    for _ in range(60):
        limiter.acquire()
    clock.t += 30.0  # thirty seconds of doing something else
    for _ in range(30):
        limiter.acquire()

    assert limiter.waits == 0, "tokens accrued while the run was busy elsewhere"


def test_a_different_provider_ceiling_is_config_not_code() -> None:
    """The next provider will have a different limit; that must not be a
    code change."""
    clock = FakeClock()
    limiter = RateLimiter(5, now=clock.now, sleep=clock.sleep)

    for _ in range(8):
        limiter.acquire()

    assert limiter.waits == 3


def test_a_nonsense_rate_is_refused() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


# -- accounting -------------------------------------------------------------


def test_requests_and_tokens_accumulate() -> None:
    instance, _ = client(
        FakeResponse("a", tokens=(100, 20)),
        FakeResponse("b", tokens=(50, 10)),
    )

    instance.complete("s", "u")
    instance.complete("s", "u")

    assert instance.usage.requests == 2
    assert instance.usage.input_tokens == 150
    assert instance.usage.output_tokens == 30


def test_usage_adds() -> None:
    total = Usage()
    total.add(Usage(requests=1, input_tokens=10, output_tokens=2))
    total.add(Usage(requests=1, input_tokens=5, output_tokens=1))
    assert (total.requests, total.input_tokens, total.output_tokens) == (2, 15, 3)


def test_a_failed_call_still_counts_against_the_limiter() -> None:
    """A request that errored was still sent, and the provider counted it."""
    clock = FakeClock()
    instance, calls = client(HttpError(500), FakeResponse("ok"), clock=clock, rpm=1)

    instance.complete("s", "u")

    assert len(calls) == 2
    assert instance.limiter.waits >= 1


def test_only_successful_calls_count_tokens() -> None:
    instance, _ = client(HttpError(503), FakeResponse("ok", tokens=(7, 3)))
    instance.complete("s", "u")
    assert instance.usage.requests == 1
    assert instance.usage.input_tokens == 7


# -- retries and classification --------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_retry(status: int) -> None:
    instance, calls = client(HttpError(status), FakeResponse("recovered"))
    assert instance.complete("s", "u").text == "recovered"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_config_errors_fail_fast(status: int) -> None:
    """A bad key or an unknown model cannot be fixed by trying again. Four
    attempts against a 240-second provider wastes sixteen minutes."""
    instance, calls = client(HttpError(status))

    with pytest.raises(LlmConfigError) as caught:
        instance.complete("s", "u")

    assert len(calls) == 1, "must not retry"
    assert "llm.model" in str(caught.value)


def test_backoff_is_exponential() -> None:
    clock = FakeClock()
    instance, _ = client(
        HttpError(500), HttpError(500), HttpError(500), FakeResponse("ok"), clock=clock
    )
    instance.complete("s", "u")
    assert clock.slept == [1.0, 2.0, 4.0]


def test_giving_up_names_the_last_failure() -> None:
    instance, calls = client(*[HttpError(500)] * 6, max_attempts=3)

    with pytest.raises(LlmError) as caught:
        instance.complete("s", "u")

    assert len(calls) == 3
    assert "500" in str(caught.value)


def test_a_missing_key_is_reported_as_unconfigured() -> None:
    """Same distinction as the YouTube key: "you have not set this" is not
    "the provider rejected you"."""
    from marginalia.config import LlmConfig

    instance = LlmClient(make_llm_config(api_key=None))
    with pytest.raises(LlmUnavailable) as caught:
        instance.complete("s", "u")
    assert "MARGINALIA_LLM_API_KEY" in str(caught.value)


def test_an_unreadable_response_is_an_error_not_a_silent_empty_string() -> None:
    instance, _ = client(object())
    with pytest.raises(LlmError):
        instance.complete("s", "u")


# -- the request itself is the portable baseline ---------------------------


def test_the_request_uses_only_the_openai_compatible_baseline() -> None:
    """No JSON mode, no tools, no prefills, no provider-specific parameters —
    every one of those is something a future provider will lack."""
    instance, calls = client(FakeResponse("ok"))

    instance.complete("system text", "user text")

    request = calls[0]
    assert set(request) == {"model", "messages", "max_tokens", "extra"}
    assert request["extra"] == {}, "nothing configured means nothing added"
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    assert request["messages"][0]["content"] == "system text"


# -- per-task request extras -----------------------------------------------


def test_a_task_block_is_merged_into_the_request() -> None:
    instance, calls = client(
        FakeResponse("ok"),
        config={"tasks": {"judge_metadata": {"reasoning_effort": "low"}}},
    )

    instance.complete("s", "u", task="judge_metadata")

    assert calls[0]["extra"] == {"reasoning_effort": "low"}


def test_llm_py_never_inspects_what_is_in_the_block() -> None:
    """Anything at all passes through. The moment this module knows what a
    field means, a provider's feature set is in the code rather than the
    config file, and swapping providers stops being config-only."""
    instance, calls = client(
        FakeResponse("ok"),
        config={"tasks": {"t": {"anything_at_all": [1, 2], "nested": {"a": True}}}},
    )

    instance.complete("s", "u", task="t")

    assert calls[0]["extra"] == {"anything_at_all": [1, 2], "nested": {"a": True}}


def test_an_unconfigured_task_gets_nothing_added() -> None:
    """A provider without the feature is configured with no task blocks, and
    then every request is exactly the portable baseline."""
    instance, calls = client(
        FakeResponse("ok"), config={"tasks": {"judge_metadata": {"reasoning_effort": "low"}}}
    )

    instance.complete("s", "u", task="write_brief")

    assert calls[0]["extra"] == {}


def test_tasks_do_not_leak_into_one_another() -> None:
    instance, calls = client(
        FakeResponse("a"),
        FakeResponse("b"),
        config={
            "tasks": {
                "judge_metadata": {"reasoning_effort": "low"},
                "write_brief": {"reasoning_effort": "high"},
            }
        },
    )

    instance.complete("s", "u", task="judge_metadata")
    instance.complete("s", "u", task="write_brief")

    assert calls[0]["extra"] == {"reasoning_effort": "low"}
    assert calls[1]["extra"] == {"reasoning_effort": "high"}


def test_mutating_the_sent_block_cannot_corrupt_the_config() -> None:
    tasks = {"t": {"reasoning_effort": "low"}}
    instance, calls = client(FakeResponse("a"), FakeResponse("b"), config={"tasks": tasks})

    instance.complete("s", "u", task="t")
    calls[0]["extra"]["reasoning_effort"] = "tampered"
    instance.complete("s", "u", task="t")

    assert calls[1]["extra"] == {"reasoning_effort": "low"}


# -- cost estimation --------------------------------------------------------


def test_cost_is_computed_from_configured_rates() -> None:
    config = make_llm_config(input_per_million=3.0, output_per_million=15.0)
    # 1M input at $3 plus 1M output at $15.
    assert config.cost_usd(1_000_000, 1_000_000) == pytest.approx(18.0)


def test_output_tokens_cost_more_than_input() -> None:
    """Reasoning tokens bill as output, which is why effort on the highest-
    volume call is where cost goes wrong fastest."""
    config = make_llm_config(input_per_million=3.0, output_per_million=15.0)
    assert config.cost_usd(0, 1000) > config.cost_usd(1000, 0)


def test_rates_come_from_config_not_code() -> None:
    config = make_llm_config(input_per_million=0.0, output_per_million=0.0)
    assert config.cost_usd(10_000_000, 10_000_000) == 0.0


def test_the_model_comes_from_config_never_a_constant() -> None:
    instance, calls = client(FakeResponse("ok"), config={"model": "some/other-model"})
    instance.complete("s", "u")
    assert calls[0]["model"] == "some/other-model"


# -- prompts ----------------------------------------------------------------


def test_every_shipped_prompt_loads_with_a_version() -> None:
    for name in (
        "write_brief",
        "review_brief",
        "expand_queries",
        "judge_metadata",
        "judge_opening",
    ):
        prompt = load_prompt(name)
        assert prompt.version, f"{name} has no version"
        assert prompt.text.strip()


def test_no_prompt_names_a_model_family() -> None:
    """Prompts must be model-agnostic: "you are Claude" framing, or anything
    tuned to one model's habits, breaks on the next provider."""
    banned = ("claude", "gpt-", "chatgpt", "gemini", "llama", "glm", "anthropic", "openai")
    for path in sorted(prompts_dir().glob("*.md")):
        body = path.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in body, f"{path.name} names a model family: {word!r}"


def test_a_prompt_renders_its_placeholders(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text(
        '---\nversion: "3"\n---\nTopic is {{ topic }} and count is {{ count }}.',
        encoding="utf-8",
    )
    prompt = load_prompt("p", directory=tmp_path)

    assert prompt.version == "3"
    assert prompt.render(topic="caching", count=9) == "Topic is caching and count is 9."


def test_an_unfilled_placeholder_is_an_error_not_a_leaked_brace(tmp_path: Path) -> None:
    """A prompt sent with `{{ brief }}` still in it produces a confident answer
    to the wrong question, which is far worse than a crash."""
    (tmp_path / "p.md").write_text(
        '---\nversion: "1"\n---\n{{ topic }} and {{ missing }}', encoding="utf-8"
    )
    with pytest.raises(LlmError) as caught:
        load_prompt("p", directory=tmp_path).render(topic="x")
    assert "missing" in str(caught.value)


def test_literal_braces_in_a_prompt_survive(tmp_path: Path) -> None:
    """Prompts contain output examples with braces. str.format would explode."""
    (tmp_path / "p.md").write_text(
        '---\nversion: "1"\n---\nReturn {"a": 1} for {{ topic }}', encoding="utf-8"
    )
    assert load_prompt("p", directory=tmp_path).render(topic="x") == 'Return {"a": 1} for x'


def test_a_prompt_without_frontmatter_is_refused(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(LlmError):
        load_prompt("p", directory=tmp_path)


def test_a_prompt_without_a_version_is_refused(tmp_path: Path) -> None:
    """The version is recorded on every artifact the prompt produces. Without
    it, "is this stale?" becomes unanswerable."""
    (tmp_path / "p.md").write_text("---\nname: p\n---\nbody", encoding="utf-8")
    with pytest.raises(LlmError) as caught:
        load_prompt("p", directory=tmp_path)
    assert "version" in str(caught.value)


# -- batching ---------------------------------------------------------------


def test_batched_splits_evenly() -> None:
    assert [list(c) for c in batched([1, 2, 3, 4], 2)] == [[1, 2], [3, 4]]


def test_batched_keeps_the_short_tail() -> None:
    assert [list(c) for c in batched([1, 2, 3], 2)] == [[1, 2], [3]]


def test_batch_size_one_yields_singletons() -> None:
    assert [list(c) for c in batched([1, 2, 3], 1)] == [[1], [2], [3]]


def test_no_provider_shaped_field_is_hardcoded() -> None:
    """A hardcoded `temperature=0` broke the first real provider swap: kimi-k3
    accepts only its own default and returned HTTP 400. Temperature looks like
    part of the OpenAI baseline and is not — reasoning models routinely refuse
    it. Only the four fields every endpoint requires are named in code;
    everything else comes from config via `extra`."""
    import inspect

    from marginalia.llm import LlmClient

    # Comments stripped: the comment above this code explains the bug by name,
    # and a test that cannot tell prose from code would forbid documenting it.
    body = "\n".join(
        line
        for line in inspect.getsource(LlmClient._send).splitlines()
        if not line.strip().startswith("#")
    )
    for field in ("temperature", "top_p", "response_format", "tools", "reasoning_effort"):
        assert f"{field}=" not in body, f"{field} must come from config, not code"


def test_a_global_extra_applies_to_every_request() -> None:
    """Where a provider-wide setting lives when one is needed — so restoring
    `temperature` is a config edit, not a code change."""
    instance, calls = client(
        FakeResponse("a"), FakeResponse("b"), config={"extra": {"temperature": 0}}
    )

    instance.complete("s", "u")
    instance.complete("s", "u", task="anything")

    assert calls[0]["extra"] == {"temperature": 0}
    assert calls[1]["extra"] == {"temperature": 0}


def test_a_task_block_overrides_the_global_key_by_key() -> None:
    instance, calls = client(
        FakeResponse("a"),
        config={
            "extra": {"temperature": 0, "top_p": 1},
            "tasks": {"t": {"temperature": 1, "reasoning_effort": "high"}},
        },
    )

    instance.complete("s", "u", task="t")

    assert calls[0]["extra"] == {"temperature": 1, "top_p": 1, "reasoning_effort": "high"}


# -- behavioural rules that live in prose ----------------------------------
#
# These are judge-bias rules, not formatting. They were each added because a
# real run went wrong in a way only reading the decision log revealed, and
# prose erodes silently under editing in a way code does not.


def test_the_metadata_judge_is_told_duration_is_not_depth() -> None:
    """Observed: the judge rejected candidates with reasons like "45-second
    short, no demonstration or real numbers possible". Sound reasoning over an
    unwarranted premise — the brief asked for implementation detail, never for
    a minimum runtime. Short may mean MAYBE; it must never mean NO on its own."""
    # Whitespace collapsed: the rule is prose and prose wraps, so a line break
    # must not be able to break the assertion that guards it.
    body = " ".join(load_prompt("judge_metadata").text.lower().split())

    assert "duration is not depth" in body
    assert "never, on its own, produce no" in body
    # The rule is worthless without the routing that makes it cheap.
    assert "maybe" in body


def test_both_judges_state_the_inclusion_bias() -> None:
    """The asymmetry is the reason the whole three-way verdict exists. If it
    ever falls out of a prompt, the judge silently starts making the
    invisible, permanent mistake instead of the cheap, visible one."""
    for name in ("judge_metadata", "judge_opening"):
        body = load_prompt(name).text.lower()
        assert "invisible" in body, f"{name} lost the asymmetry argument"
        assert "permanent" in body, f"{name} lost the asymmetry argument"


def test_the_metadata_judge_rejects_other_depth_proxies_too() -> None:
    """Duration was the one observed. View count and channel size are the same
    inference and would be just as invisible."""
    body = load_prompt("judge_metadata").text.lower()
    assert "view count" in body
    assert "channel size" in body
