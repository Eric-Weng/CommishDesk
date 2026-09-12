"""Story 3.2 — ``commishdesk/narrate/llm.py``: the opt-in LLM narrator.

One test (or group) per I/O & Edge-Case Matrix row, plus the structural guards
the Boundaries section calls for: aggregator-free imports (AD-15), lazy SDK
imports (``import commishdesk.narrate`` pulls in no ``anthropic`` / ``google.genai``
/ ``httpx``), the payload is the ``narration`` projection and nothing else
(AD-1), and no model-id / endpoint literal lives under ``narrate/``.

The two provider adapters are exercised end to end by injecting a **fake**
``anthropic`` / ``google.genai`` module into ``sys.modules`` — no CI job installs
the real ``llm`` extra, so without the fakes only the "SDK not importable" branch
would ever run.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from commishdesk import demo
from commishdesk.errors import NarratorError
from commishdesk.facts import build_draft_recap_facts
from commishdesk.facts.schema import (
    HeadlineNumbers,
    Narration,
    NarrationLeague,
    NarrationTeam,
    PositionalRunsSummary,
    QBRunSummary,
    RBRunSummary,
    Superlatives,
    TERunSummary,
)
from commishdesk.ingest import build_league_model
from commishdesk.llmconfig import LLMConfig, LLMModelConfig, load_llm_config
from commishdesk.narrate import (
    NarrationResult,
    build_narration_payload,
    narrate_draft_recap,
    narrate_with_llm,
    recap_to_text,
    render_draft_recap,
)
from commishdesk.narrate.llm import (
    RETRY_CAP,
    AnthropicClient,
    GoogleClient,
    _raise_if_transient_anthropic,
    _raise_if_transient_google,
    _TransientProviderError,
    build_client,
)
from commishdesk.stats import (
    compute_board_metrics,
    compute_consensus_metrics,
    compute_draft_grades,
)

LLM_PKG = Path(__file__).resolve().parent.parent / "commishdesk" / "narrate" / "llm.py"
NARRATE_DIR = LLM_PKG.parent
CONFIG = LLMConfig(
    primary=LLMModelConfig("anthropic", "claude-sonnet-5"),
    fallback=LLMModelConfig("google", "gemini-3.5-flash"),
)


# --------------------------------------------------------------------------- #
# fakes — LLMClient level
# --------------------------------------------------------------------------- #


@dataclass
class FakeVoice:
    system_prompt: str = "You are a mild beat writer for a fantasy league."
    banned_topics: frozenset[str] = frozenset()


@dataclass
class FakeClient:
    reply: str = "a voiced recap"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def generate(self, payload: str, voice: object) -> str:
        self.calls.append((payload, getattr(voice, "system_prompt", "")))
        return self.reply


@dataclass
class BoomClient:
    calls: list[str] = field(default_factory=list)

    def generate(self, payload: str, voice: object) -> str:
        self.calls.append(payload)
        raise RuntimeError("provider is down")


@dataclass
class TransientClient:
    """Raises a transient provider fault ``fail_times`` times, then returns ``reply``.

    Stands in for a provider that times out / 429s a few times before recovering
    — the retry loop's unit-level fake. The real adapter-side classification of an
    SDK exception into ``_TransientProviderError`` is covered separately.
    """

    fail_times: int
    reply: str = "recovered prose"
    calls: list[str] = field(default_factory=list)

    def generate(self, payload: str, voice: object) -> str:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_times:
            raise _TransientProviderError("simulated request timeout")
        return self.reply


@dataclass
class BadReturnClient:
    """Returns something that is not usable text — must count as a failed attempt."""

    value: object = "   "
    calls: list[str] = field(default_factory=list)

    def generate(self, payload: str, voice: object) -> object:
        self.calls.append(payload)
        return self.value


@dataclass
class ExplodingFactory:
    """A ``client_factory`` that fails the test if it is ever called."""

    def __call__(self, cfg: LLMModelConfig) -> object:
        raise AssertionError("client_factory must not be called when llm_enabled=False")


class SeqFactory:
    """Hands out pre-built clients in call order and records the configs seen."""

    def __init__(self, *clients: object) -> None:
        self._clients = list(clients)
        self.seen: list[LLMModelConfig] = []

    def __call__(self, cfg: LLMModelConfig) -> object:
        client = self._clients[len(self.seen)]
        self.seen.append(cfg)
        return client


# --------------------------------------------------------------------------- #
# fakes — provider SDK modules
# --------------------------------------------------------------------------- #


def _install_fake_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: str = "anthropic prose",
    stop_reason: str = "end_turn",
    sink: dict[str, list[dict[str, object]]],
) -> None:
    module = types.ModuleType("anthropic")

    class _Messages:
        def create(self, **kwargs: object) -> object:
            sink.setdefault("create", []).append(kwargs)
            return types.SimpleNamespace(
                stop_reason=stop_reason,
                content=[types.SimpleNamespace(type="text", text=reply)],
            )

    class _Anthropic:
        def __init__(self, **kwargs: object) -> None:
            sink.setdefault("client", []).append(kwargs)
            self.messages = _Messages()

    module.Anthropic = _Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)


def _install_fake_genai(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: str = "google prose",
    finish_reason: str | None = "STOP",
    sink: dict[str, list[dict[str, object]]],
) -> None:
    genai = types.ModuleType("google.genai")

    class _Models:
        def generate_content(self, **kwargs: object) -> object:
            sink.setdefault("generate", []).append(kwargs)
            candidate = types.SimpleNamespace(
                finish_reason=(
                    None if finish_reason is None
                    else types.SimpleNamespace(name=finish_reason)
                )
            )
            return types.SimpleNamespace(text=reply, candidates=[candidate])

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            sink.setdefault("client", []).append(kwargs)
            self.models = _Models()

    genai.Client = _Client  # type: ignore[attr-defined]
    google_pkg = types.ModuleType("google")
    google_pkg.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai)


# --------------------------------------------------------------------------- #
# narration fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def narration() -> Narration:
    model = build_league_model(demo.load_demo_bundle())
    board = compute_board_metrics(model)
    consensus = compute_consensus_metrics(model, demo.demo_consensus_slots())
    grades = compute_draft_grades(model, consensus)
    doc = build_draft_recap_facts(
        model,
        board,
        consensus,
        grades,
        generated_at="2026-09-07T00:00:00Z",
        consensus_source_name=demo.DEMO_CONSENSUS_SOURCE_NAME,
        consensus_as_of=demo.DEMO_CONSENSUS_AS_OF,
    )
    return doc.narration


def synthetic_narration(n_teams: int) -> Narration:
    runs = PositionalRunsSummary(
        QB=QBRunSummary(total=0, by_end_round3=0),
        RB=RBRunSummary(total=0, in_round1=0),
        TE=TERunSummary(total=0),
    )
    return Narration(
        league=NarrationLeague(name="Synthetic League", season="2025", scoring_label="PPR"),
        headline_numbers=HeadlineNumbers(picks_total=n_teams, first_window_rb_count=0),
        superlatives=Superlatives(),
        teams=[
            NarrationTeam(manager=f"mgr{i}", roster_id=str(i), pick_count=1, grade="B")
            for i in range(n_teams)
        ],
        positional_runs=runs,
    )


# --------------------------------------------------------------------------- #
# Matrix: happy path
# --------------------------------------------------------------------------- #


def test_happy_path_uses_primary_with_exactly_one_call(narration: Narration) -> None:
    primary = FakeClient("primary prose")
    factory = SeqFactory(primary)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert isinstance(result, NarrationResult)
    assert result == NarrationResult(text="primary prose", narrator="llm-primary")
    assert len(primary.calls) == 1
    assert factory.seen == [CONFIG.primary]
    payload, system_prompt = primary.calls[0]
    assert payload == narration.model_dump_json()
    assert system_prompt == FakeVoice().system_prompt


# --------------------------------------------------------------------------- #
# Matrix: primary fails -> fallback
# --------------------------------------------------------------------------- #


def test_primary_failure_falls_through_to_fallback_once(narration: Narration) -> None:
    primary, fallback = BoomClient(), FakeClient("fallback prose")
    factory = SeqFactory(primary, fallback)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert result == NarrationResult(text="fallback prose", narrator="llm-fallback")
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1
    assert factory.seen == [CONFIG.primary, CONFIG.fallback]


def test_bad_return_value_counts_as_a_failed_attempt(narration: Narration) -> None:
    """A misbehaving client that returns non-``str`` / empty must not become an
    empty ``NarrationResult`` — it falls through like a raised exception would."""
    for bad in ("   ", "", None, 42, ["nope"]):
        primary = BadReturnClient(value=bad)
        fallback = FakeClient("clean fallback")
        result = narrate_draft_recap(
            narration,
            FakeVoice(),
            CONFIG,
            llm_enabled=True,
            client_factory=SeqFactory(primary, fallback),
        )
        assert result == NarrationResult(text="clean fallback", narrator="llm-fallback")
        assert len(primary.calls) == 1


def test_both_returning_bad_values_degrades_to_template(narration: Narration) -> None:
    result = narrate_draft_recap(
        narration,
        FakeVoice(),
        CONFIG,
        llm_enabled=True,
        client_factory=SeqFactory(BadReturnClient(value=None), BadReturnClient(value="")),
    )
    assert result.narrator == "template"
    assert result.text == recap_to_text(render_draft_recap(narration))


# --------------------------------------------------------------------------- #
# Matrix: both fail -> template, never raises
# --------------------------------------------------------------------------- #


def test_both_providers_failing_returns_template_text(narration: Narration) -> None:
    factory = SeqFactory(BoomClient(), BoomClient())
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert result.narrator == "template"
    assert result.text == recap_to_text(render_draft_recap(narration))


def test_narrate_with_llm_raises_when_both_fail_chained(narration: Narration) -> None:
    with pytest.raises(NarratorError) as excinfo:
        narrate_with_llm(
            narration,
            FakeVoice(),
            CONFIG,
            client_factory=SeqFactory(BoomClient(), BoomClient()),
        )
    assert isinstance(excinfo.value.__cause__, RuntimeError)


# --------------------------------------------------------------------------- #
# Matrix: transient provider fault -> retried on the same provider (FR-40 / 3.6)
# --------------------------------------------------------------------------- #


def test_retry_cap_is_a_named_module_constant() -> None:
    from commishdesk.narrate import llm

    assert isinstance(llm.RETRY_CAP, int)
    assert llm.RETRY_CAP == 2
    assert "RETRY_CAP = 2" in LLM_PKG.read_text(encoding="utf-8")


def test_retry_loop_is_immediate_no_backoff_no_clock() -> None:
    """The Never-clause: retries carry no ``sleep`` / backoff / clock dep. Pinned
    structurally so a future 'just add a small sleep' cannot slip in."""
    src = LLM_PKG.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported.isdisjoint({"time", "asyncio", "datetime"})
    # no call to *.sleep(...) or a bare sleep(...) anywhere in the module
    calls = [
        node.func
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    names = {f.attr for f in calls if isinstance(f, ast.Attribute)} | {
        f.id for f in calls if isinstance(f, ast.Name)
    }
    assert "sleep" not in names


def test_transient_primary_fault_is_retried_exactly_at_the_cap_then_succeeds(
    narration: Narration,
) -> None:
    """Boundary: fails on every attempt but the last permitted one — an
    off-by-one in ``range(1 + RETRY_CAP)`` would regress here."""
    primary = TransientClient(fail_times=RETRY_CAP, reply="recovered at the cap")
    factory = SeqFactory(primary)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert result == NarrationResult(
        text="recovered at the cap", narrator="llm-primary"
    )
    assert len(primary.calls) == 1 + RETRY_CAP
    assert factory.seen == [CONFIG.primary]


def test_transient_primary_fault_is_retried_then_succeeds(narration: Narration) -> None:
    primary = TransientClient(fail_times=1, reply="primary recovered")
    factory = SeqFactory(primary)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert result == NarrationResult(text="primary recovered", narrator="llm-primary")
    assert len(primary.calls) == 2  # one transient fault + one success
    assert factory.seen == [CONFIG.primary]  # never advanced to the fallback


def test_transient_primary_every_attempt_falls_through_to_fallback(
    narration: Narration,
) -> None:
    primary = TransientClient(fail_times=99)
    fallback = FakeClient("fallback prose")
    factory = SeqFactory(primary, fallback)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert result == NarrationResult(text="fallback prose", narrator="llm-fallback")
    assert len(primary.calls) == 1 + RETRY_CAP  # exactly the cap, then give up
    assert len(fallback.calls) == 1
    assert factory.seen == [CONFIG.primary, CONFIG.fallback]  # one build per provider


def test_transient_on_both_providers_degrades_to_template(narration: Narration) -> None:
    primary, fallback = TransientClient(fail_times=99), TransientClient(fail_times=99)
    result = narrate_draft_recap(
        narration,
        FakeVoice(),
        CONFIG,
        llm_enabled=True,
        client_factory=SeqFactory(primary, fallback),
    )
    assert result.narrator == "template"
    assert result.text == recap_to_text(render_draft_recap(narration))
    assert len(primary.calls) == 1 + RETRY_CAP
    assert len(fallback.calls) == 1 + RETRY_CAP


def test_transient_both_raises_chained_from_the_transient_signal(
    narration: Narration,
) -> None:
    with pytest.raises(NarratorError) as excinfo:
        narrate_with_llm(
            narration,
            FakeVoice(),
            CONFIG,
            client_factory=SeqFactory(
                TransientClient(fail_times=99), TransientClient(fail_times=99)
            ),
        )
    assert isinstance(excinfo.value.__cause__, _TransientProviderError)


def test_non_transient_primary_fault_is_not_retried(narration: Narration) -> None:
    """Pins today's behaviour: a non-transient error (here a plain ``RuntimeError``)
    is tried exactly once before the fallback — no retry, no change from baseline."""
    primary, fallback = BoomClient(), FakeClient("fallback prose")
    factory = SeqFactory(primary, fallback)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert result == NarrationResult(text="fallback prose", narrator="llm-fallback")
    assert len(primary.calls) == 1  # exactly once
    assert factory.seen == [CONFIG.primary, CONFIG.fallback]


def test_unusable_completion_is_not_retried(narration: Narration) -> None:
    """An empty / non-``str`` completion is non-transient: one attempt, fall through."""
    primary = BadReturnClient(value="   ")
    fallback = FakeClient("fallback prose")
    factory = SeqFactory(primary, fallback)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=factory
    )
    assert result.narrator == "llm-fallback"
    assert len(primary.calls) == 1


# --------------------------------------------------------------------------- #
# COMMISHDESK_LLM_TIMEOUT -> forwarded to each provider SDK client (FR-40 / 3.6)
# --------------------------------------------------------------------------- #


def test_timeout_is_forwarded_to_the_anthropic_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_anthropic(monkeypatch, sink=sink)
    AnthropicClient("m", env={"LLM_API_KEY": "k"}, timeout=30.0).generate(
        "{}", FakeVoice()
    )
    assert sink["client"][0]["timeout"] == 30.0


def test_timeout_is_converted_to_ms_for_the_google_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_genai(monkeypatch, sink=sink)
    GoogleClient("m", env={"GEMINI_API_KEY": "k"}, timeout=30.0).generate(
        "{}", FakeVoice()
    )
    assert sink["client"][0]["http_options"] == {"timeout": 30000}


def test_google_sub_millisecond_timeout_floors_to_one_ms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tiny timeout must not round to 0 ms — google-genai reads 0 as "no timeout"."""
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_genai(monkeypatch, sink=sink)
    GoogleClient("m", env={"GEMINI_API_KEY": "k"}, timeout=0.0004).generate(
        "{}", FakeVoice()
    )
    assert sink["client"][0]["http_options"]["timeout"] == 1


def test_google_endpoint_and_timeout_merge_into_one_http_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_genai(monkeypatch, sink=sink)
    GoogleClient(
        "m", endpoint="https://gw/google", env={"GOOGLE_API_KEY": "k"}, timeout=30.0
    ).generate("{}", FakeVoice())
    assert sink["client"][0]["http_options"] == {
        "base_url": "https://gw/google",
        "timeout": 30000,
    }


def test_no_timeout_leaves_the_sdk_default_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_anthropic(monkeypatch, sink=sink)
    AnthropicClient("m", env={"LLM_API_KEY": "k"}).generate("{}", FakeVoice())
    assert "timeout" not in sink["client"][0]


def test_build_client_threads_timeout_from_config() -> None:
    cfg = load_llm_config({"COMMISHDESK_LLM_TIMEOUT": "45"})
    assert build_client(cfg.primary).timeout == 45.0  # type: ignore[union-attr]
    assert build_client(cfg.fallback).timeout == 45.0  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Matrix: an SDK exception is classified transient (429 / 5xx / timeout / transport)
# or non-transient (other 4xx / anything unrecognized -> falls through, always safe)
# --------------------------------------------------------------------------- #


class _FakeAPITimeoutError(Exception):
    pass


class _FakeAPIConnectionError(Exception):
    pass


class _FakeRateLimitError(Exception):
    pass


class _FakeAPIStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


_FAKE_ANTHROPIC = types.SimpleNamespace(
    APITimeoutError=_FakeAPITimeoutError,
    APIConnectionError=_FakeAPIConnectionError,
    RateLimitError=_FakeRateLimitError,
    APIStatusError=_FakeAPIStatusError,
)


@pytest.mark.parametrize(
    "exc",
    [
        _FakeAPITimeoutError("slow"),
        _FakeAPIConnectionError("reset"),
        _FakeRateLimitError("429"),
        _FakeAPIStatusError(408),
        _FakeAPIStatusError(429),
        _FakeAPIStatusError(503),
        _FakeAPIStatusError(500),
    ],
)
def test_anthropic_transient_faults_are_reraised_as_transient(exc: Exception) -> None:
    with pytest.raises(_TransientProviderError):
        _raise_if_transient_anthropic(exc, _FAKE_ANTHROPIC)


@pytest.mark.parametrize(
    "exc",
    [_FakeAPIStatusError(400), _FakeAPIStatusError(404), RuntimeError("weird")],
)
def test_anthropic_non_transient_faults_return_silently(exc: Exception) -> None:
    # returns without raising -> the adapter re-raises the original, no retry
    _raise_if_transient_anthropic(exc, _FAKE_ANTHROPIC)


class _FakeServerError(Exception):
    pass


class _FakeGoogleAPIError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


_FAKE_GENAI_ERRORS = types.SimpleNamespace(
    ServerError=_FakeServerError, APIError=_FakeGoogleAPIError
)


@pytest.mark.parametrize(
    "exc",
    [
        _FakeServerError("boom"),
        _FakeGoogleAPIError(408),
        _FakeGoogleAPIError(429),
        _FakeGoogleAPIError(502),
    ],
)
def test_google_transient_faults_are_reraised_as_transient(exc: Exception) -> None:
    with pytest.raises(_TransientProviderError):
        _raise_if_transient_google(exc, _FAKE_GENAI_ERRORS)


@pytest.mark.parametrize("exc", [_FakeGoogleAPIError(400), RuntimeError("weird")])
def test_google_non_transient_faults_return_silently(exc: Exception) -> None:
    _raise_if_transient_google(exc, _FAKE_GENAI_ERRORS)
    _raise_if_transient_google(exc, None)  # errors module unimportable -> still safe


def test_raw_httpx_transport_fault_is_transient_for_either_provider() -> None:
    import httpx

    fault = httpx.ConnectError("connection refused")
    with pytest.raises(_TransientProviderError):
        _raise_if_transient_anthropic(fault, _FAKE_ANTHROPIC)
    with pytest.raises(_TransientProviderError):
        _raise_if_transient_google(fault, _FAKE_GENAI_ERRORS)


def test_anthropic_adapter_maps_an_sdk_timeout_to_a_transient_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the adapter: the SDK call raises, the adapter classifies
    it, and ``narrate_with_llm`` would retry it."""
    module = types.ModuleType("anthropic")
    module.APITimeoutError = _FakeAPITimeoutError  # type: ignore[attr-defined]

    class _Messages:
        def create(self, **kwargs: object) -> object:
            raise _FakeAPITimeoutError("request timed out")

    class _Anthropic:
        def __init__(self, **kwargs: object) -> None:
            self.messages = _Messages()

    module.Anthropic = _Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)

    with pytest.raises(_TransientProviderError):
        AnthropicClient("m", env={"LLM_API_KEY": "k"}).generate("{}", FakeVoice())


def test_google_adapter_maps_an_sdk_error_to_a_transient_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the adapter: a ``google.genai`` ``ServerError`` from the
    SDK call is classified transient — pins the ``except`` block in
    ``GoogleClient.generate`` (deleting it would not otherwise fail the suite)."""
    genai = types.ModuleType("google.genai")
    errors_mod = types.ModuleType("google.genai.errors")
    errors_mod.ServerError = _FakeServerError  # type: ignore[attr-defined]
    errors_mod.APIError = _FakeGoogleAPIError  # type: ignore[attr-defined]

    class _Models:
        def generate_content(self, **kwargs: object) -> object:
            raise _FakeServerError("upstream 503")

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            self.models = _Models()

    genai.Client = _Client  # type: ignore[attr-defined]
    genai.errors = errors_mod  # type: ignore[attr-defined]
    google_pkg = types.ModuleType("google")
    google_pkg.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_mod)

    with pytest.raises(_TransientProviderError):
        GoogleClient("m", env={"GEMINI_API_KEY": "k"}).generate("{}", FakeVoice())


# --------------------------------------------------------------------------- #
# Matrix: LLM disabled
# --------------------------------------------------------------------------- #


def test_llm_disabled_constructs_no_client_and_imports_no_sdk(narration: Narration) -> None:
    result = narrate_draft_recap(
        narration,
        FakeVoice(),
        CONFIG,
        llm_enabled=False,
        client_factory=ExplodingFactory(),
    )
    assert result.narrator == "template"
    assert result.text == recap_to_text(render_draft_recap(narration))
    assert "anthropic" not in sys.modules
    assert "google.genai" not in sys.modules


# --------------------------------------------------------------------------- #
# Matrix: config override -> build_client produces the matching adapter
# --------------------------------------------------------------------------- #


def test_build_client_maps_provider_to_adapter() -> None:
    anthropic_cfg = LLMModelConfig("anthropic", "claude-haiku-4-5", endpoint="https://x")
    google_cfg = LLMModelConfig("google", "gemini-2.5-flash")
    a = build_client(anthropic_cfg)
    g = build_client(google_cfg)
    assert isinstance(a, AnthropicClient) and a.model_id == "claude-haiku-4-5"
    assert a.endpoint == "https://x"
    assert isinstance(g, GoogleClient) and g.model_id == "gemini-2.5-flash"


def test_config_override_changes_no_narrate_file() -> None:
    """A model swap must stay a config-only change under ``narrate/`` — with one
    narrow, documented exception (Story 4.6 / Spec Change Log): ``pricing.py``'s
    ``MODEL_PRICES`` necessarily names the two ``llmconfig`` defaults literally so
    an unpriced model can fail closed *by name* rather than silently mis-price.
    Every other file under ``narrate/`` still carries no model-id literal."""
    cfg = load_llm_config({"COMMISHDESK_LLM_FALLBACK": "anthropic:claude-haiku-4-5"})
    assert isinstance(build_client(cfg.fallback), AnthropicClient)
    forbidden = ("claude-sonnet-5", "gemini-3.5-flash", "claude-haiku-4-5", "gemini-")
    for path in sorted(NARRATE_DIR.glob("*.py")):
        if path.name == "pricing.py":
            continue
        src = path.read_text(encoding="utf-8")
        assert not any(token in src for token in forbidden), path.name


# --------------------------------------------------------------------------- #
# Adapters: SDK not importable -> NarratorError chained from ImportError
# --------------------------------------------------------------------------- #


def test_missing_sdk_raises_narrator_error_chained_from_importerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # force the ImportError branch deterministically, regardless of whether the
    # real `llm` extra happens to be installed in this environment
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setitem(sys.modules, "google", None)
    monkeypatch.setitem(sys.modules, "google.genai", None)

    with pytest.raises(NarratorError) as a_exc:
        AnthropicClient("claude-sonnet-5", env={"ANTHROPIC_API_KEY": "sk"}).generate(
            "{}", FakeVoice()
        )
    assert isinstance(a_exc.value.__cause__, ImportError)
    with pytest.raises(NarratorError) as g_exc:
        GoogleClient("gemini-3.5-flash", env={"GEMINI_API_KEY": "k"}).generate(
            "{}", FakeVoice()
        )
    assert isinstance(g_exc.value.__cause__, ImportError)


def test_unusable_providers_degrade_to_template(
    narration: Narration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setitem(sys.modules, "google", None)
    monkeypatch.setitem(sys.modules, "google.genai", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=build_client
    )
    assert result.narrator == "template"
    assert result.text == recap_to_text(render_draft_recap(narration))


# --------------------------------------------------------------------------- #
# Adapters: driven through a fake SDK module (P1 / P5)
# --------------------------------------------------------------------------- #


def test_anthropic_adapter_missing_key_is_not_an_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_anthropic(monkeypatch, sink={})
    with pytest.raises(NarratorError) as exc:
        AnthropicClient("m", env={}).generate("{}", FakeVoice())
    assert not isinstance(exc.value.__cause__, ImportError)


@pytest.mark.parametrize("key_var", ["ANTHROPIC_API_KEY", "LLM_API_KEY"])
def test_anthropic_adapter_generates_with_each_accepted_key(
    monkeypatch: pytest.MonkeyPatch, key_var: str
) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_anthropic(monkeypatch, reply="the recap", sink=sink)
    out = AnthropicClient("claude-x", env={key_var: "sk-secret"}).generate(
        "PAYLOAD", FakeVoice("SYS")
    )
    assert out == "the recap"
    assert sink["client"][0]["api_key"] == "sk-secret"
    assert "base_url" not in sink["client"][0]
    created = sink["create"][0]
    assert created["model"] == "claude-x"
    assert created["system"] == "SYS"
    assert created["messages"] == [{"role": "user", "content": "PAYLOAD"}]


def test_anthropic_adapter_forwards_endpoint_as_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_anthropic(monkeypatch, sink=sink)
    AnthropicClient("m", endpoint="https://gw/anthropic", env={"LLM_API_KEY": "k"}).generate(
        "{}", FakeVoice()
    )
    assert sink["client"][0]["base_url"] == "https://gw/anthropic"


@pytest.mark.parametrize("reply", ["", "   \n\t "])
def test_anthropic_adapter_empty_completion_raises(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    _install_fake_anthropic(monkeypatch, reply=reply, sink={})
    with pytest.raises(NarratorError):
        AnthropicClient("m", env={"LLM_API_KEY": "k"}).generate("{}", FakeVoice())


def test_anthropic_adapter_truncated_completion_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_anthropic(monkeypatch, reply="cut off here", stop_reason="max_tokens", sink={})
    with pytest.raises(NarratorError):
        AnthropicClient("m", env={"LLM_API_KEY": "k"}).generate("{}", FakeVoice())


def test_google_adapter_missing_key_is_not_an_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_genai(monkeypatch, sink={})
    with pytest.raises(NarratorError) as exc:
        GoogleClient("m", env={}).generate("{}", FakeVoice())
    assert not isinstance(exc.value.__cause__, ImportError)


@pytest.mark.parametrize("key_var", ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
def test_google_adapter_generates_with_each_accepted_key(
    monkeypatch: pytest.MonkeyPatch, key_var: str
) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_genai(monkeypatch, reply="gemini recap", sink=sink)
    out = GoogleClient("gemini-x", env={key_var: "g-secret"}).generate("PAYLOAD", FakeVoice("SYS"))
    assert out == "gemini recap"
    assert sink["client"][0]["api_key"] == "g-secret"
    assert "http_options" not in sink["client"][0]
    gen = sink["generate"][0]
    assert gen["model"] == "gemini-x"
    assert gen["contents"] == "PAYLOAD"
    assert gen["config"]["system_instruction"] == "SYS"  # type: ignore[index]


def test_google_adapter_forwards_endpoint_as_http_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: dict[str, list[dict[str, object]]] = {}
    _install_fake_genai(monkeypatch, sink=sink)
    GoogleClient("m", endpoint="https://gw/google", env={"GOOGLE_API_KEY": "k"}).generate(
        "{}", FakeVoice()
    )
    assert sink["client"][0]["http_options"] == {"base_url": "https://gw/google"}


@pytest.mark.parametrize("reply", ["", "  \n "])
def test_google_adapter_empty_completion_raises(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    _install_fake_genai(monkeypatch, reply=reply, sink={})
    with pytest.raises(NarratorError):
        GoogleClient("m", env={"GEMINI_API_KEY": "k"}).generate("{}", FakeVoice())


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION"])
def test_google_adapter_unclean_finish_reason_raises(
    monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    _install_fake_genai(monkeypatch, reply="partial text", finish_reason=finish, sink={})
    with pytest.raises(NarratorError):
        GoogleClient("m", env={"GEMINI_API_KEY": "k"}).generate("{}", FakeVoice())


def test_google_adapter_stop_and_absent_finish_reason_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_genai(monkeypatch, reply="ok text", finish_reason="STOP", sink={})
    assert GoogleClient("m", env={"GEMINI_API_KEY": "k"}).generate("{}", FakeVoice()) == "ok text"
    _install_fake_genai(monkeypatch, reply="ok text 2", finish_reason=None, sink={})
    assert (
        GoogleClient("m", env={"GEMINI_API_KEY": "k"}).generate("{}", FakeVoice())
        == "ok text 2"
    )


def test_fake_sdk_adapter_plugs_into_the_selector(
    narration: Narration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: real ``build_client`` + fake anthropic module -> llm-primary."""
    _install_fake_anthropic(monkeypatch, reply="voiced by the fake sdk", sink={})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    result = narrate_draft_recap(
        narration, FakeVoice(), CONFIG, llm_enabled=True, client_factory=build_client
    )
    assert result == NarrationResult(text="voiced by the fake sdk", narrator="llm-primary")


# --------------------------------------------------------------------------- #
# Matrix: member count varies -> still exactly one successful generation call
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n_teams", [4, 200])
def test_member_count_does_not_change_the_call_count(n_teams: int) -> None:
    primary = FakeClient("prose")
    factory = SeqFactory(primary)
    result = narrate_draft_recap(
        synthetic_narration(n_teams),
        FakeVoice(),
        CONFIG,
        llm_enabled=True,
        client_factory=factory,
    )
    assert result.narrator == "llm-primary"
    assert len(primary.calls) == 1


# --------------------------------------------------------------------------- #
# Fallback logging is the operator's only "we spent nothing" signal (P9)
# --------------------------------------------------------------------------- #


def test_primary_failure_emits_a_warning(
    narration: Narration, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="commishdesk"):
        narrate_draft_recap(
            narration,
            FakeVoice(),
            CONFIG,
            llm_enabled=True,
            client_factory=SeqFactory(BoomClient(), FakeClient("fb")),
        )
    assert [r for r in caplog.records if r.levelno == logging.WARNING], caplog.text


def test_both_failing_and_falling_to_template_emits_a_warning(
    narration: Narration, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="commishdesk"):
        result = narrate_draft_recap(
            narration,
            FakeVoice(),
            CONFIG,
            llm_enabled=True,
            client_factory=SeqFactory(BoomClient(), BoomClient()),
        )
    assert result.narrator == "template"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, caplog.text
    assert any("template narrator" in r.getMessage() for r in warnings)


# --------------------------------------------------------------------------- #
# Payload is the narration projection and nothing else (AD-1)
# --------------------------------------------------------------------------- #


def test_payload_is_exactly_the_narration_projection(narration: Narration) -> None:
    payload = build_narration_payload(narration)
    decoded = json.loads(payload)
    assert set(decoded) == set(Narration.model_fields)
    for leaked in ("box_score", "roster", "rosters", "model_rank", "picks", "draft_picks"):
        assert leaked not in decoded


# --------------------------------------------------------------------------- #
# Structural: no aggregator, lazy SDK imports, no SDK on `import commishdesk.narrate`
# --------------------------------------------------------------------------- #


def _module_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_llm_module_names_no_aggregator_only_direct_sdks() -> None:
    tree = ast.parse(LLM_PKG.read_text(encoding="utf-8"))
    imported = _module_imports(tree)
    roots = {name.split(".")[0] for name in imported}
    for aggregator in ("openai", "litellm", "openrouter", "llama_index", "langchain"):
        assert aggregator not in roots
    provider_roots = roots & {"anthropic", "google"}
    assert provider_roots == {"anthropic", "google"}


def test_provider_sdk_imports_are_all_function_local() -> None:
    tree = ast.parse(LLM_PKG.read_text(encoding="utf-8"))
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"anthropic", "google"}
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in {"anthropic", "google"}


def test_importing_commishdesk_narrate_pulls_in_no_sdk_or_httpx() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import commishdesk.narrate, sys; "
            "bad = {'anthropic', 'google.genai', 'httpx'} & sys.modules.keys(); "
            "assert not bad, sorted(bad); print('ok')",
        ],
        capture_output=True,
        text=True,
        cwd=str(LLM_PKG.parents[2]),
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "ok"
