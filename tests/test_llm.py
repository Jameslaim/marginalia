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

import logging

from pathlib import Path

import pytest
from conftest import make_llm_config

from marginalia.contracts import Usage
from marginalia.llm import (
    LlmBillingError,
    LlmClient,
    LlmConfigError,
    LlmRateLimited,
    LlmError,
    LlmUnavailable,
    RateLimiter,
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


def test_a_402_stops_on_the_first_call() -> None:
    """Measured, on the note drain that found this. An account with no balance
    was asked four times per video for three videos: twelve calls, every one of
    which could only fail, and unlike a rate limit the condition does not clear
    in seconds. A 402 is the provider being authoritative, the same way Google's
    429 is authoritative about quota whatever the local counter reads."""
    instance, calls = client(HttpError(402))

    with pytest.raises(LlmBillingError) as caught:
        instance.complete("s", "u")

    assert len(calls) == 1, "must not retry"
    assert "cannot pay" in str(caught.value)
    assert "llm.api_key" not in str(caught.value), (
        "the key is fine and the balance is not; sending the reader to the key "
        "is the generic message being actively wrong"
    )


def test_a_billing_error_is_a_config_error() -> None:
    """Every caller that already stops on "you must fix this" stops on this too,
    without learning a new type."""
    assert issubclass(LlmBillingError, LlmConfigError)


@pytest.mark.parametrize("status", [402, 405, 413, 418, 451])
def test_an_unrecognised_4xx_is_not_retried(status: int) -> None:
    """The default used to be to retry, and 402 fell through it.

    A 4xx means the request was refused, and an identical request is refused
    identically. The exceptions are named in `_RETRY_STATUSES` and are exactly
    the ones that clear on their own."""
    instance, calls = client(HttpError(status))

    with pytest.raises(LlmConfigError):
        instance.complete("s", "u")

    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 409, 429])
def test_the_retryable_4xx_still_retry(status: int) -> None:
    """The other half of the rule above: a timeout, a conflict and a rate limit
    are 4xx that a second attempt genuinely fixes."""
    instance, calls = client(HttpError(status), FakeResponse("recovered"))

    assert instance.complete("s", "u").text == "recovered"
    assert len(calls) == 2


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
        "write_note",
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
    """A hardcoded `temperature=0` can break a provider swap: reasoning-oriented
    providers may accept only their own default and return HTTP 400. Temperature
    looks like part of the OpenAI baseline and is not — reasoning models
    routinely refuse it. Only the four fields every endpoint requires are named
    in code; everything else comes from config via `extra`."""
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


def test_every_request_carries_a_distinct_user_value() -> None:
    """xKiro replays byte-identical blocking requests within a short window and
    offers no opt-out header: five identical calls returned one distinct answer,
    four of them in under 0.4s. An in-run retry of a failed note would then be
    handed the same failure back instead of a fresh sample. A unique `user` per
    call is what makes two otherwise identical requests differ in their bytes.

    `user` is named in code rather than coming from `extra` because config is
    static and this value must not be — which is the whole reason
    rule 7 was widened to allow it rather than quietly broken."""
    from marginalia.llm import _call_id

    values = {_call_id() for _ in range(64)}
    assert len(values) == 64


def test_the_user_value_is_not_an_identity() -> None:
    """It exists to bust a cache, not to name anyone. Sending a real identifier
    would put one on every request for no benefit and could become a tracking
    key by accident."""
    import getpass
    import os

    from marginalia.llm import _call_id

    value = _call_id()
    for secret in (getpass.getuser(), os.environ.get("USERNAME") or "no-such-name"):
        assert secret.lower() not in value.lower()


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


def test_the_note_prompt_demands_a_marker_on_anything_inferred() -> None:
    """An audit of five notes found ~44 outside-knowledge assertions written in
    the video's own voice, against 2 marked. M6 answers from notes, so an
    unmarked inference becomes an asserted fact two layers from any transcript
    that could contradict it."""
    # Whitespace collapsed: the rule is prose, prose wraps, and a line break
    # must not be able to break the assertion guarding it.
    body = " ".join(load_prompt("write_note").text.lower().split())

    assert "[note:" in body, "the marker convention itself is gone"
    assert "if it did not come out of the transcript" in body
    # The four things that must carry it. Arithmetic and outside knowledge were
    # the two observed; extrapolation and comparison are the same mistake.
    for kind in ("arithmetic you performed", "extrapolation", "outside knowledge", "comparison"):
        assert kind in body, f"the marker no longer covers {kind}"


def test_the_note_prompt_does_not_ban_inference() -> None:
    """Banning it would be worse than the problem. "The video never states
    break-even, but its own figures give ~1.3 reads" is more useful than
    silence — what is forbidden is it reading as something the speaker said."""
    body = " ".join(load_prompt("write_note").text.lower().split())

    assert "your own reasoning is welcome" in body
    assert "indistinguishable" in body


def test_the_note_prompt_names_the_two_sections_that_leak_most() -> None:
    """The audit found inference concentrated in Leaves out and Wrong or shaky,
    for a structural reason: saying "the video never covers X" requires knowing
    X, and the explanation of X that follows is the note's, not the video's."""
    body = " ".join(load_prompt("write_note").text.lower().split())

    assert "the section where this goes wrong most is **leaves out**" in body
    assert "the same applies to **wrong or shaky**" in body


def test_the_note_prompt_pins_timestamps_to_where_a_claim_starts() -> None:
    """Measured across 23 citations in five notes: 12 landed late, mean +3.7s,
    worst +7s. Deep links are M6's primary output and mid-sentence is wrong for
    quoting."""
    body = " ".join(load_prompt("write_note").text.lower().split())

    assert "start timestamp of the transcript line where the claim begins" in body
    assert "not where the sentence finishes" in body


def test_the_note_prompt_asks_for_a_deliberate_undefined_terms_pass() -> None:
    """A recall miss, not a rule failure: "breakpoint" was used undefined and
    never reached Assumes you know. By the end of a transcript every term in it
    feels familiar, which is exactly why it needs a separate pass."""
    body = " ".join(load_prompt("write_note").text.lower().split())

    assert "deliberate pass" in body
    assert "without ever being defined" in body


def test_the_note_prompt_lists_every_required_section_and_states_no_count() -> None:
    """The prompt said "exactly these seven sections" and listed eight, from the
    commit that added `How current is it` until v8. The list is the count: a
    number beside it is a second statement of the same fact that can drift."""
    from marginalia.enrich.note import REQUIRED_SECTIONS

    text = load_prompt("write_note").text
    for name in REQUIRED_SECTIONS:
        assert f"\n## {name}" in text.replace("\r\n", "\n"), f"the prompt does not list {name!r}"
    body = " ".join(text.lower().split())
    for word in ("six", "seven", "eight", "nine", "ten"):
        assert f"exactly these {word} sections" not in body
    assert "output exactly these sections, in this order" in body


def test_the_metadata_judge_rejects_other_depth_proxies_too() -> None:
    """Duration was the one observed. View count and channel size are the same
    inference and would be just as invisible."""
    body = load_prompt("judge_metadata").text.lower()
    assert "view count" in body
    assert "channel size" in body


# -- Retry-After, and the request id --------------------------------------
#
# Both of these came out of asking marginalia's own library how production
# agent systems handle tool failures. It named two things this codebase did not
# do: honour the header a rate-limited server sends, and capture the one field
# a provider needs to investigate anything.


class HttpErrorWithHeaders(HttpError):
    """A status error carrying response headers, as a real SDK exception does."""

    def __init__(self, status_code: int, headers: dict | None = None, body: str = "") -> None:
        super().__init__(status_code)
        if body:
            self.args = (f"Error code: {status_code} - {body}",)
        self.response = type("R", (), {"headers": headers or {}})()


def test_retry_after_seconds_is_honoured_over_the_backoff() -> None:
    """A rate limit is the one failure where the provider states the answer, so
    guessing at it is strictly worse than reading it."""
    clock = FakeClock()
    instance, calls = client(
        HttpErrorWithHeaders(429, {"retry-after": "5"}), FakeResponse("ok"), clock=clock
    )

    assert instance.complete("s", "u").text == "ok"
    assert len(calls) == 2
    assert clock.slept == [5.0], "the header, not the 1s first backoff"


def test_retry_after_as_an_http_date_is_honoured() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=8))
    clock = FakeClock()
    instance, _ = client(
        HttpErrorWithHeaders(429, {"retry-after": when}), FakeResponse("ok"), clock=clock
    )

    assert instance.complete("s", "u").text == "ok"
    assert 5.0 <= clock.slept[0] <= 9.0, "the date, read as a delay"


def test_a_date_in_the_past_means_retry_now_not_a_negative_wait() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    clock = FakeClock()
    instance, _ = client(
        HttpErrorWithHeaders(429, {"retry-after": when}), FakeResponse("ok"), clock=clock
    )

    instance.complete("s", "u")
    assert clock.slept == [0.0]


def test_an_unreadable_retry_after_falls_back_to_the_backoff() -> None:
    """Unparseable is *absent*, never zero. Reading a malformed hint as "wait 0s"
    turns it into a hot loop against a server that just asked for room."""
    clock = FakeClock()
    instance, _ = client(
        HttpErrorWithHeaders(429, {"retry-after": "soon-ish"}), FakeResponse("ok"), clock=clock
    )

    instance.complete("s", "u")
    assert clock.slept == [1.0], "the ordinary first backoff"


def test_a_wait_longer_than_the_run_stops_instead_of_hanging() -> None:
    """Rule 24's judgement: a server answering "3600" is telling you to come back
    after the run is over, which is a refusal in a different costume."""
    clock = FakeClock()
    instance, calls = client(HttpErrorWithHeaders(429, {"retry-after": "3600"}), clock=clock)

    with pytest.raises(LlmRateLimited) as caught:
        instance.complete("s", "u")

    assert len(calls) == 1, "no second attempt, and no hour-long sleep"
    assert clock.slept == []
    assert "3600s wait" in str(caught.value)


def test_a_rate_limit_stop_is_a_config_error() -> None:
    """So every caller that already stops on "you must fix this" stops here too."""
    assert issubclass(LlmRateLimited, LlmConfigError)


def test_the_request_id_is_logged_from_the_header(caplog) -> None:
    clock = FakeClock()
    instance, _ = client(
        HttpErrorWithHeaders(500, {"x-request-id": "req_abc123"}),
        FakeResponse("ok"),
        clock=clock,
    )
    with caplog.at_level(logging.INFO, logger="marginalia.llm"):
        instance.complete("s", "u")

    assert "req_abc123" in caplog.text


def test_a_missing_request_id_is_reported_as_missing(caplog) -> None:
    """An empty field is a complete answer. A body is not."""
    clock = FakeClock()
    instance, _ = client(HttpErrorWithHeaders(500), FakeResponse("ok"), clock=clock)
    with caplog.at_level(logging.INFO, logger="marginalia.llm"):
        instance.complete("s", "u")

    assert "not reported" in caplog.text


def test_an_error_body_never_reaches_the_log(caplog) -> None:
    """**The test that matters.** Provider error bodies echo the request, and the
    request here is a system prompt plus an entire transcript — hard rule 1's
    worst case, in the file that accumulates every run and that nobody opens
    until something is already wrong.

    The obvious way to guarantee the request id is in the log is to log the whole
    body. This asserts that road stays closed.
    """
    secret = "the speaker says the cache TTL is five minutes and then contradicts himself"
    failure = HttpErrorWithHeaders(
        500,
        {"x-request-id": "req_xyz"},
        body=f"{{'error': {{'message': 'bad input', 'echo': '{secret}'}}}}",
    )
    clock = FakeClock()
    instance, _ = client(failure, FakeResponse("ok"), clock=clock)

    with caplog.at_level(logging.DEBUG, logger="marginalia.llm"):
        instance.complete("s", secret)

    assert "req_xyz" in caplog.text, "the id is there"
    assert secret not in caplog.text, "and the transcript is not"
    assert "echo" not in caplog.text
