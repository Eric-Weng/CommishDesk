"""Invariant tests I1-I7 (AD-22 / ``CLAUDE.md`` §2).

Each invariant named in ``CLAUDE.md`` §2 gets exactly one named test here
(``test_I1`` … ``test_I7``). An agent may not delete a test, loosen its assertion,
mark it ``xfail``, or route around it. Until an invariant's epic lands, its test is an
explicit ``pytest.skip("pending Epic N")`` whose docstring quotes the invariant verbatim
from ``CLAUDE.md`` §2 -- that skip is the only acceptable non-passing state, and it is
removed (not weakened) when the epic implements it.

FR-49 fixes the invariant → epic assignment: Epic 2 (I1, I4), Epic 3 (I3),
Epic 7 (I2, I5, I6, I7). ``test_invariant_registry_is_complete_and_unweakened`` machine-
checks the "may not delete or weaken an invariant test" rule.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_I1() -> None:
    """I1 — No paid operation executes for a league with zero verified channels.
    The Generation Set is *derived* from verified-channel state by exactly one
    constructor. No other code path may add a league to a run."""
    pytest.skip("pending Epic 2")


def test_I2() -> None:
    """I2 — An address receives at most one unsolicited message, ever.
    One pending confirmation per address, globally, for its lifetime; a second request
    for the same address is a silent no-op with a byte-identical response."""
    pytest.skip("pending Epic 7")


def test_I3() -> None:
    """I3 — LLM cost per league-week is exactly one call. Regardless of member
    count. The Recap is generated once per league and reused for every recipient."""
    pytest.skip("pending Epic 3")


def test_I4() -> None:
    """I4 — Deterministic output requires no credentials and no paid resources.
    `ingest → stats → facts → narrate(template) → render` runs with zero credentials and
    no network beyond the Sleeper API. The onboarding sample is stats + templated prose
    only — no LLM. Two runs on one frozen input produce byte-identical output (modulo the
    generated-at timestamp)."""
    pytest.skip("pending Epic 2")


def test_I5() -> None:
    """I5 — Continued delivery requires continued engagement.
    Zero opens and zero clicks for N consecutive weeks → automatic deactivation until
    re-engaged; a deactivated league generates no Issues and no spend; status is computed
    live from engagement history, never stored as a flag."""
    pytest.skip("pending Epic 7")


def test_I6() -> None:
    """I6 — Total run cost is computed before any spend.
    The weekly job derives its complete work list, prices it, then runs fully or not at
    all. Over the configured ceiling → hard abort + operator alert, zero spend. No code
    path discovers an overrun mid-run."""
    pytest.skip("pending Epic 7")


def test_I7() -> None:
    """I7 — Transactional and bulk mail use separate sending identities.
    Confirmations on one subdomain, newsletters on another, so a reputation hit on one
    cannot take down the other."""
    pytest.skip("pending Epic 7")


# invariant → FR-49 epic number
INVARIANT_EPICS = {"I1": 2, "I2": 7, "I3": 3, "I4": 2, "I5": 7, "I6": 7, "I7": 7}


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def _claude_md_headlines() -> dict[str, str] | None:
    """``{"I1": "I1 — <headline up to first period>", …}`` parsed from ``CLAUDE.md`` §2,
    or ``None`` when ``CLAUDE.md`` is absent (out-of-tree: extracted sdist / wheel)."""
    path = REPO_ROOT / "CLAUDE.md"
    if not path.is_file():
        return None
    section = re.split(r"\n##\s+3\.", path.read_text(encoding="utf-8"), maxsplit=1)[0]
    section = section.split("## 2.", 1)[1]
    out: dict[str, str] = {}
    for n in range(1, 8):
        m = re.search(rf"\*\*(I{n}\b[^\n]*?)\.\*\*", section)
        assert m, f"CLAUDE.md §2 has no bolded headline for I{n}"
        out[f"I{n}"] = _normalize_ws(m.group(1))
    return out


def test_invariant_registry_is_complete_and_unweakened() -> None:
    """Registry guard — machine-checks the "may not delete or weaken an invariant test"
    rule. The module defines precisely ``test_I1`` … ``test_I7`` in numeric order with no
    extras or gaps; each has a non-empty docstring quoting its ``CLAUDE.md`` §2 headline
    verbatim; no function source contains ``xfail`` / ``skipif`` and no module-level
    ``pytestmark`` exists; and each stub is *either* already a clean pass *or* a
    ``pytest.skip`` whose message is exactly ``pending Epic <FR-49 number>`` — so an epic
    graduating its stub to a real passing test does not trip this guard (``CLAUDE.md``
    §2: the skip "is removed (not weakened) when the epic implements it")."""
    module = sys.modules[__name__]
    invariant_tests = {
        name: obj
        for name, obj in vars(module).items()
        if re.fullmatch(r"test_I\d+", name) and callable(obj)
    }

    # exactly test_I1..test_I7, numeric order, no extras / gaps
    assert set(invariant_tests) == {f"test_I{n}" for n in range(1, 8)}
    by_line = sorted(invariant_tests.values(), key=lambda f: f.__code__.co_firstlineno)
    assert [f.__name__ for f in by_line] == [f"test_I{n}" for n in range(1, 8)]
    assert set(INVARIANT_EPICS) == {f"I{n}" for n in range(1, 8)}

    # no epic may weaken an invariant test by marking the whole module xfail/skip
    assert getattr(module, "pytestmark", None) in (None, [], ()), "module-level pytestmark"

    headlines = _claude_md_headlines()  # None only when out-of-tree

    for n in range(1, 8):
        key = f"I{n}"
        fn = invariant_tests[f"test_{key}"]
        doc = fn.__doc__ or ""

        assert doc.strip(), f"test_{key}: empty docstring"
        if headlines is not None:
            assert headlines[key] in _normalize_ws(doc), (
                f"test_{key}: docstring no longer quotes the CLAUDE.md §2 headline "
                f"{headlines[key]!r}"
            )

        # no xfail / skipif marker, no assertion-loosening decorator
        assert not hasattr(fn, "pytestmark"), f"test_{key}: carries a pytest marker"
        try:
            src = inspect.getsource(fn)
        except OSError:  # pragma: no cover - source always present in this repo
            src = ""
        assert "xfail" not in src and "skipif" not in src, f"test_{key}: weakened"

        # EITHER a clean pass (epic landed) OR the exact pending-epic skip — nothing else
        expected_skip = f"pending Epic {INVARIANT_EPICS[key]}"
        try:
            fn()
        except pytest.skip.Exception as exc:
            assert exc.msg == expected_skip, (
                f"test_{key}: skip message {exc.msg!r} is not {expected_skip!r}"
            )
        except Exception as exc:  # noqa: BLE001 - a failing invariant test is the signal
            raise AssertionError(f"test_{key} raised {exc!r}; expected pass or skip") from exc
