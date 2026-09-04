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

import ast
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# retro A3: names that count as real enforcement even without a literal `assert` -- a
# graduated invariant test may check its property through one of these.
_ASSERTION_HELPERS = frozenset(
    {"raises", "fail", "assert_frame_equal", "assert_series_equal"}
)


class _AssertionFinder(ast.NodeVisitor):
    """retro A3 / P4: does a function body carry real enforcement? A non-constant
    ``assert``, a ``raise AssertionError(...)``, a ``pytest.raises`` block, a
    ``pytest.fail(...)`` call, or a named assertion helper. Nested ``def`` / ``async def``
    / ``lambda`` scopes are NOT descended into -- a dead inner ``assert`` doesn't count.
    ``assert True`` / ``assert 1`` (a constant test) doesn't count either."""

    def __init__(self) -> None:
        self.found = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: D102
        return  # do not descend into a nested function

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: D102
        return

    def visit_Assert(self, node: ast.Assert) -> None:  # noqa: D102
        if not isinstance(node.test, ast.Constant):
            self.found = True

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: D102
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = (
            exc.id
            if isinstance(exc, ast.Name)
            else exc.attr
            if isinstance(exc, ast.Attribute)
            else ""
        )
        if name == "AssertionError":
            self.found = True

    def visit_Call(self, node: ast.Call) -> None:  # noqa: D102
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if name in _ASSERTION_HELPERS:
            self.found = True
        self.generic_visit(node)


def _strip_docstring(fn_node: ast.FunctionDef) -> list[ast.stmt]:
    body = list(fn_node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _body_has_assertion(fn_node: ast.FunctionDef) -> bool:
    """True when the body (docstring excluded) carries real enforcement -- see
    ``_AssertionFinder``. A bare ``pass`` that kept only its docstring is exactly the
    "route around it" weakening ``CLAUDE.md`` §2 forbids."""
    finder = _AssertionFinder()
    for stmt in _strip_docstring(fn_node):
        finder.visit(stmt)
    return finder.found


def _ast_skip_message(fn_node: ast.FunctionDef) -> str | None:
    """The literal message of a ``pytest.skip("...")`` / ``skip("...")`` call in the body,
    or ``None`` when the body has no such call. retro P4: lets the registry guard classify
    a ``test_I{n}`` that takes a pytest fixture (and so cannot be called with no args)
    without executing it."""
    for stmt in _strip_docstring(fn_node):
        for sub in ast.walk(stmt):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name == "skip" and sub.args and isinstance(sub.args[0], ast.Constant):
                return sub.args[0].value
    return None


def test_I1() -> None:
    """I1 — No paid operation executes for a league with zero verified channels.
    The Generation Set is *derived* from verified-channel state by exactly one
    constructor. No other code path may add a league to a run."""
    from commishdesk import generation
    from commishdesk.generation import GenerationSet, build_generation_set

    # empty in -> empty out; 10k harvested ids, none activated -> empty out, zero work
    assert build_generation_set([]).league_ids == ()
    assert (
        build_generation_set(
            (str(n) for n in range(10_000)), is_activated=lambda _id: False
        ).league_ids
        == ()
    )
    assert isinstance(build_generation_set(["77", "77"]), GenerationSet)
    assert build_generation_set(["77", "77"]).league_ids == ("77",)

    # exactly one module in the package constructs GenerationSet(...)
    pkg_root = Path(generation.__file__).resolve().parent
    builders = sorted(
        path.relative_to(pkg_root).as_posix()
        for path in pkg_root.rglob("*.py")
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "GenerationSet"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
    assert builders == ["generation.py"], builders


def test_I2() -> None:
    """I2 — An address receives at most one unsolicited message, ever.
    One pending confirmation per address, globally, for its lifetime; a second request
    for the same address is a silent no-op with a byte-identical response."""
    pytest.skip("pending Epic 7")


def test_I3() -> None:
    """I3 — LLM cost per league-week is exactly one call. Regardless of member
    count. The Recap is generated once per league and reused for every recipient."""
    pytest.skip("pending Epic 3")


def test_I4(monkeypatch: "pytest.MonkeyPatch") -> None:
    """I4 — Deterministic output requires no credentials and no paid resources.
    `ingest → stats → facts → narrate(template) → render` runs with zero credentials and
    no network beyond the Sleeper API. The onboarding sample is stats + templated prose
    only — no LLM. Two runs on one frozen input produce byte-identical output (modulo the
    generated-at timestamp)."""
    from datetime import datetime, timezone

    from commishdesk import demo
    from commishdesk.facts import build_draft_recap_facts
    from commishdesk.ingest import build_league_model
    from commishdesk.narrate import recap_to_text, render_draft_recap
    from commishdesk.render import recap_to_html
    from commishdesk.stats import (
        compute_board_metrics,
        compute_consensus_metrics,
        compute_draft_grades,
    )

    # no credential of any kind is consulted by the deterministic core
    for key in ("LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    def _demo_chain() -> tuple[str, str]:
        model = build_league_model(demo.load_demo_bundle())
        board = compute_board_metrics(model)
        consensus = compute_consensus_metrics(model, demo.demo_consensus_slots())
        grades = compute_draft_grades(model, consensus)
        doc = build_draft_recap_facts(
            model,
            board,
            consensus,
            grades,
            generated_at=datetime.now(tz=timezone.utc),
            consensus_source_name="synthetic rookie board",
            consensus_as_of="2025-05",
        )
        recap = render_draft_recap(doc.narration)
        return recap_to_text(recap), recap_to_html(recap)

    text1, html1 = _demo_chain()
    text2, html2 = _demo_chain()
    assert text1 == text2, "template narrator stdout is not deterministic"
    assert html1 == html2, "rendered HTML is not deterministic"
    # the narrator itself reads no clock — its only stamp is the caller's generated_at
    assert "generated" not in text1

    # httpx is only reachable on the real-league path — the narrator pulls in none
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import commishdesk.narrate.template as t, sys; t.render_draft_recap; "
            "assert 'httpx' not in sys.modules; print('ok')",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "ok"


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

    # retro A3: parse this module once so a graduated (non-skipping) test_I{n} can be
    # checked for a real assertion in its body.
    _tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    fn_nodes = {
        node.name: node
        for node in _tree.body
        if isinstance(node, ast.FunctionDef) and re.fullmatch(r"test_I\d+", node.name)
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
        node = fn_nodes[f"test_{key}"]
        args = node.args
        takes_fixture = bool(
            args.args
            or args.posonlyargs
            or args.kwonlyargs
            or args.vararg
            or args.kwarg
        )

        graduated_msg = (
            f"test_{key}: graduated (does not skip) but its body carries no assertion "
            f"- a real `assert`, `raise AssertionError`, `pytest.raises`/`pytest.fail`, "
            f"or a named assertion helper is required"
        )

        if takes_fixture:
            # retro P4: a graduated test_I{n} may take a pytest fixture (e.g. tmp_path),
            # so it cannot be called with no args. Classify it from the AST instead:
            # a pytest.skip("...") call in the body => still the pending-epic skip case;
            # otherwise it is graduated and must carry a real assertion.
            skip_msg = _ast_skip_message(node)
            if skip_msg is not None:
                assert skip_msg == expected_skip, (
                    f"test_{key}: skip message {skip_msg!r} is not {expected_skip!r}"
                )
            else:
                assert _body_has_assertion(node), graduated_msg
            continue

        try:
            fn()
        except pytest.skip.Exception as exc:
            assert exc.msg == expected_skip, (
                f"test_{key}: skip message {exc.msg!r} is not {expected_skip!r}"
            )
        except Exception as exc:  # noqa: BLE001 - a failing invariant test is the signal
            raise AssertionError(f"test_{key} raised {exc!r}; expected pass or skip") from exc
        else:
            # retro A3: a clean pass means the epic graduated this stub to a real test —
            # it must then actually assert its invariant. A bodyless `pass` that kept only
            # its docstring is the precise weakening this guard exists to catch.
            assert _body_has_assertion(node), graduated_msg


def _first_func(src: str) -> ast.FunctionDef:
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_body_has_assertion_unit() -> None:
    """Committed regression test for retro A3 / P4. On a clean tree all seven stubs skip,
    so ``_body_has_assertion`` is never reached by the registry guard -- this exercises it
    directly over the cases the parallel review called out."""
    yes = {
        "plain assert": "def t():\n    assert x == 1\n",
        "assert with message": 'def t():\n    assert f(), "boom"\n',
        "pytest.raises block": "def t():\n    with pytest.raises(ValueError):\n        f()\n",
        "raise AssertionError": 'def t():\n    raise AssertionError("x")\n',
        "pytest.fail": 'def t():\n    pytest.fail("x")\n',
        "named helper": "def t():\n    assert_frame_equal(a, b)\n",
        "assert after setup": "def t():\n    v = compute()\n    assert v == 3\n",
    }
    for label, src in yes.items():
        assert _body_has_assertion(_first_func(src)) is True, label

    no = {
        "bare pass": "def t():\n    pass\n",
        "docstring only": 'def t():\n    """just a docstring"""\n',
        "assert True": "def t():\n    assert True\n",
        "assert 1": "def t():\n    assert 1\n",
        "dead nested def": "def t():\n    def _inner():\n        assert x == 1\n    return None\n",
        "dead lambda": "def t():\n    g = lambda: (_ for _ in ()).throw(AssertionError())\n",
        "just a call": "def t():\n    do_something()\n",
    }
    for label, src in no.items():
        assert _body_has_assertion(_first_func(src)) is False, label
