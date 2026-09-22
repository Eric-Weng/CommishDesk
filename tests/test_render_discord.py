"""Story 4.3 — ``render_discord_summary`` (deterministic plain-text Discord post).

Mirrors ``tests/test_render_email.py``: byte-determinism across two calls, a first
line naming league + season, ``< 2000`` chars, no ``\\r\\n``, no embed / image
markup, both narrator paths, the empty-``lead_candidates`` fallback, the
neither / both ``ValueError``, and adversarial-string / bidi handling.

Committed tests never read ``../brief/`` — the render input is the committed demo
oracle fixture.
"""

from __future__ import annotations

import ast
import json
import sys

import pytest

from commishdesk.facts.schema import DraftRecapFacts
from commishdesk.narrate import Recap, Section, render_draft_recap
from commishdesk.narrate.weekly_template import WeeklyIssue, WeeklySection
from commishdesk.render import render_discord_summary
from commishdesk.render.discord import _escape_discord_markdown, render_weekly_discord_summary
from tests.conftest import REPO_ROOT

EXPECTED_FACTS_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-draft-recap-facts.json"
)


def _facts() -> DraftRecapFacts:
    return DraftRecapFacts.model_validate(
        json.loads(EXPECTED_FACTS_PATH.read_text(encoding="utf-8"))
    )


def _recap(facts: DraftRecapFacts) -> Recap:
    return render_draft_recap(facts.narration)


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_first_line_names_league_and_season() -> None:
    facts = _facts()
    out = render_discord_summary(facts, recap=_recap(facts))
    first = out.splitlines()[0]
    assert first == f"{facts.league.name} — {facts.league.season} Draft Recap"


def test_summary_is_plain_text_under_2000_chars_lf_only() -> None:
    facts = _facts()
    out = render_discord_summary(facts, recap=_recap(facts))
    assert len(out) < 2000
    assert "\r" not in out
    # no embed / image / hosted-link markup
    for token in ("<svg", "<img", "http://", "https://", "```", "embed"):
        assert token not in out


def test_body_has_a_blank_line_then_a_lead_sentence() -> None:
    facts = _facts()
    out = render_discord_summary(facts, recap=_recap(facts))
    lines = out.split("\n")
    assert lines[1] == ""
    assert lines[2].strip()  # the lead sentence
    # the lead sentence is drawn from the narrated Lead's first block
    collapsed = " ".join(_recap(facts).sections[0].blocks[0].split())
    assert collapsed.startswith(lines[2].rstrip("…"))


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_byte_identical_across_two_renders_both_paths() -> None:
    facts = _facts()
    assert render_discord_summary(facts, recap=_recap(facts)) == render_discord_summary(
        facts, recap=_recap(facts)
    )
    llm = "## The Lead\n\nA sentence about the draft.\n\n## Team Grades\n\nMore."
    assert render_discord_summary(facts, llm_text=llm) == render_discord_summary(
        facts, llm_text=llm
    )


def test_no_clock_or_rng_value_in_the_output() -> None:
    facts = _facts()
    out = render_discord_summary(facts, recap=_recap(facts))
    assert "generated" not in out  # no provenance stamp on this surface


# --------------------------------------------------------------------------- #
# Narrator paths + edges
# --------------------------------------------------------------------------- #


def test_llm_path_uses_the_first_prose_block_not_a_leading_heading() -> None:
    facts = _facts()
    llm = "## The Lead\n\nOpening prose here.\n\n## Grades\n\nGrades prose."
    out = render_discord_summary(facts, llm_text=llm)
    assert out.splitlines()[0] == (
        f"{facts.league.name} — {facts.league.season} Draft Recap"
    )
    assert "Opening prose here." in out
    assert "## The Lead" not in out


def test_empty_lead_candidates_still_produces_a_sentence_from_the_body() -> None:
    base = _facts()
    facts = base.model_copy(
        update={
            "narration": base.narration.model_copy(update={"lead_candidates": []})
        }
    )
    out = render_discord_summary(facts, recap=render_draft_recap(facts.narration))
    assert out.splitlines()[0].endswith("Draft Recap")
    assert len(out.splitlines()) >= 3
    assert out.splitlines()[2].strip()


def test_recap_with_an_empty_lead_section_falls_back_to_the_title_only() -> None:
    facts = _facts()
    recap = Recap(
        title="ignored",
        dateline="ignored",
        sections=[Section(heading="The Lead", blocks=[]), Section(heading="X", blocks=["y"])],
    )
    out = render_discord_summary(facts, recap=recap)
    assert out == f"{facts.league.name} — {facts.league.season} Draft Recap"


def test_long_lead_is_truncated_at_a_word_boundary() -> None:
    facts = _facts()
    long_block = " ".join(["word"] * 400)
    recap = Recap(
        title="ignored",
        dateline="ignored",
        sections=[Section(heading="The Lead", blocks=[long_block])],
    )
    out = render_discord_summary(facts, recap=recap)
    assert len(out) < 2000
    assert out.endswith("…")
    assert "word word" in out


def test_newline_in_league_name_still_yields_a_single_first_line() -> None:
    base = _facts()
    facts = base.model_copy(
        update={"league": base.league.model_copy(update={"name": "Trench\nWarfare\r\n"})}
    )
    out = render_discord_summary(facts, recap=render_draft_recap(facts.narration))
    assert "\r" not in out
    first = out.split("\n", 1)[0]
    assert first == f"Trench Warfare — {facts.league.season} Draft Recap"
    assert out.count("\n") == 2  # exactly the one blank-line separator


def test_pathologically_long_title_is_clamped_under_2000_with_an_ellipsis() -> None:
    base = _facts()
    facts = base.model_copy(
        update={"league": base.league.model_copy(update={"name": "L " * 2000})}
    )
    out = render_discord_summary(facts, recap=render_draft_recap(facts.narration))
    assert len(out) < 2000
    assert out.endswith("…")
    assert "\r" not in out


def test_neither_or_both_bodies_raise_value_error() -> None:
    facts = _facts()
    with pytest.raises(ValueError, match="neither"):
        render_discord_summary(facts)
    with pytest.raises(ValueError, match="both"):
        render_discord_summary(facts, recap=_recap(facts), llm_text="x")


# --------------------------------------------------------------------------- #
# Adversarial strings / bidi
# --------------------------------------------------------------------------- #

_ADVERSARIAL = 'A<b>& "Team" 😀 ‮evil'


def test_bidi_controls_are_stripped_from_the_output() -> None:
    base = _facts()
    facts = base.model_copy(
        update={"league": base.league.model_copy(update={"name": _ADVERSARIAL})}
    )
    recap = Recap(
        title="ignored",
        dateline="ignored",
        sections=[Section(heading="The Lead", blocks=[_ADVERSARIAL])],
    )
    out = render_discord_summary(facts, recap=recap)
    for cp in (0x200E, 0x200F, 0x202A, 0x202E, 0x2066, 0x2069):
        assert chr(cp) not in out
    # plain text: < and & are literal (not a markup surface)
    assert 'A<b>& "Team"' in out


# --------------------------------------------------------------------------- #
# AD-1 import fence
# --------------------------------------------------------------------------- #


def test_discord_render_module_imports_only_the_sanctioned_surface() -> None:
    tree = ast.parse(
        (REPO_ROOT / "commishdesk" / "render" / "discord.py").read_text(encoding="utf-8")
    )
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    external = roots - sys.stdlib_module_names
    assert external <= {"commishdesk"}, external


def test_discord_render_import_pulls_in_no_sdk_and_no_httpx() -> None:
    import subprocess

    prog = (
        "import sys;"
        "import commishdesk.render.discord as d;"
        "from commishdesk.render import render_discord_summary;"
        "assert d.render_discord_summary is render_discord_summary;"
        "bad = {'anthropic', 'google.genai', 'httpx'} & sys.modules.keys();"
        "assert not bad, sorted(bad);"
        "fence = {'commishdesk.ingest', 'commishdesk.stats', 'commishdesk.adapters'} "
        "& sys.modules.keys();"
        "assert not fence, sorted(fence);"
        "print('ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", prog], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# --------------------------------------------------------------------------- #
# Story 5.11a — render_weekly_discord_summary / _escape_discord_markdown
# --------------------------------------------------------------------------- #


def test_escape_discord_markdown_escapes_the_backslash_before_the_rest() -> None:
    assert _escape_discord_markdown("plain text") == "plain text"
    assert _escape_discord_markdown("a\\b") == "a\\\\b"
    assert _escape_discord_markdown("*bold* _it_ ~x~ `c` |t| >q #head") == (
        "\\*bold\\* \\_it\\_ \\~x\\~ \\`c\\` \\|t\\| \\>q \\#head"
    )


def test_render_weekly_discord_summary_shape() -> None:
    issue = WeeklyIssue(
        title="Trench Warfare — Week 10 Recap",
        dateline="ignored",
        sections=[
            WeeklySection(heading="The Lead", blocks=["Week 10: six games played."]),
            WeeklySection(heading="Around the League", blocks=["x"]),
        ],
    )
    out = render_weekly_discord_summary(issue, week=10)
    lines = out.split("\n")
    assert lines[0] == "Trench Warfare — Week 10 Recap"
    assert lines[1] == ""
    assert lines[2] == "Week 10: six games played."
    assert len(out) < 2000
    assert "\r" not in out


def test_render_weekly_discord_summary_clips_a_long_lead() -> None:
    long_lead = " ".join(["word"] * 400)
    issue = WeeklyIssue(
        title="L — Week 10 Recap",
        dateline="ignored",
        sections=[WeeklySection(heading="The Lead", blocks=[long_lead])],
    )
    out = render_weekly_discord_summary(issue, week=10)
    assert len(out) < 2000
    assert out.endswith("…")


def test_render_weekly_discord_summary_escapes_markdown() -> None:
    issue = WeeklyIssue(
        title="Trench *Warfare* — Week 10 Recap",
        dateline="ignored",
        sections=[WeeklySection(heading="The Lead", blocks=["plain."])],
    )
    out = render_weekly_discord_summary(issue, week=10)
    assert "\\*Warfare\\*" in out
    assert "*Warfare*" not in out


def test_render_weekly_discord_summary_falls_back_to_a_week_title() -> None:
    out = render_weekly_discord_summary(WeeklyIssue(title="", dateline="x"), week=3)
    assert out == "Week 3 Recap"


def test_render_weekly_discord_summary_escapes_before_clipping_stays_under_2000() -> None:
    """The reviewed-and-fixed ordering bug (edge-case-hunter, step-04 review):
    escaping grows the string (one backslash per control char), so escaping
    *after* the 2000-char clip could push an already-under-the-limit summary
    back over it. A lead saturated with control characters, long enough to
    clip, proves the escaped output still respects the limit."""
    hostile_lead = " ".join(["*bold*"] * 300)  # far past 2000 chars once escaped
    issue = WeeklyIssue(
        title="L — Week 10 Recap",
        dateline="ignored",
        sections=[WeeklySection(heading="The Lead", blocks=[hostile_lead])],
    )
    out = render_weekly_discord_summary(issue, week=10)
    assert len(out) < 2000
    assert "*" not in out.replace("\\*", "")  # every real "*" was escaped, none bare
