"""Story 8.15 — ``render/charts_static``: chart-panel PNGs and the social cards.

Offline (a blocked socket proves it), matplotlib-free import checks that always run,
and the real render tests, which need the opt-in ``charts`` extra and skip without it
(the ``charts`` CI job runs them). Inputs are the committed weekly Facts fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import struct
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from commishdesk.facts.schema import WeeklyFacts
from tests.conftest import REPO_ROOT

FACTS_DIR = REPO_ROOT / "tests" / "fixtures" / "facts"
MODULE = REPO_ROOT / "commishdesk" / "render" / "charts_static.py"
HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None
needs_charts = pytest.mark.skipif(not HAS_MATPLOTLIB, reason="the charts extra is not installed")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _raw(week: str = "10") -> dict[str, Any]:
    return json.loads((FACTS_DIR / f"expected-weekly-facts-week{week}.json").read_text(encoding="utf-8"))


def _facts(week: str = "10") -> WeeklyFacts:
    return WeeklyFacts.model_validate(_raw(week))


def _size(png: bytes) -> tuple[int, int]:
    assert png[:8] == _PNG_MAGIC
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _chunks(png: bytes) -> list[str]:
    names, pos = [], 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        names.append(png[pos + 4 : pos + 8].decode("ascii"))
        pos += 12 + length
    return names


# --------------------------------------------------------------------------- #
# Always run: the core install stays matplotlib-free
# --------------------------------------------------------------------------- #

_BLOCKED = textwrap.dedent(
    """
    import json
    import sys

    sys.modules["matplotlib"] = None  # any `import matplotlib` raises ImportError
    import commishdesk
    import commishdesk.render
    from commishdesk.facts.schema import WeeklyFacts
    from commishdesk.render import charts_static as cs

    facts = WeeklyFacts.model_validate(json.load(open(sys.argv[1], encoding="utf-8")))
    try:
        cs.render_cards(facts)
    except cs.ChartsUnavailableError:
        print("unavailable")
    else:
        raise SystemExit("render_cards must raise without matplotlib")
    """
)


def test_cards_report_unavailable_without_matplotlib() -> None:
    fixture = FACTS_DIR / "expected-weekly-facts-week10.json"
    done = subprocess.run(
        [sys.executable, "-c", _BLOCKED, str(fixture)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "unavailable"


def test_importing_commishdesk_does_not_import_matplotlib() -> None:
    code = (
        "import sys, commishdesk, commishdesk.render, commishdesk.render.charts_static;"
        "sys.exit(1 if 'matplotlib' in sys.modules else 0)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    assert done.returncode == 0, done.stderr


def test_matplotlib_is_an_extra_not_a_core_dependency() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    core = re.search(r"^dependencies = \[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert core is not None and "matplotlib" not in core.group(1)
    assert re.search(r"^charts = \[\s*\"matplotlib", text, re.MULTILINE)


def test_module_carries_no_hand_typed_color_and_no_top_level_matplotlib_import() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", source), "colors come from render/style.py tokens"
    assert not re.search(r"^(import|from) matplotlib", source, re.MULTILINE)


def test_fonts_ship_as_ttf_with_their_licences() -> None:
    fonts = REPO_ROOT / "commishdesk" / "render" / "fonts"
    for name in (
        "BricolageGrotesque-ExtraBold.ttf",
        "DMSans-Regular.ttf",
        "DMSans-Bold.ttf",
        "IBMPlexMono-Medium.ttf",
    ):
        assert (fonts / name).read_bytes()[:4] in (b"\x00\x01\x00\x00", b"true"), name
    for licence in ("OFL-BricolageGrotesque.txt", "OFL-DMSans.txt", "OFL-IBMPlexMono.txt"):
        assert (fonts / licence).is_file()


def test_week_one_has_no_panels() -> None:
    from commishdesk.render import charts_static as cs

    assert cs.available_panels(_facts("01")) == []
    assert cs.available_panels(_facts("10")) == ["luck", "power"]


# --------------------------------------------------------------------------- #
# The finding sentence (pure Python, no matplotlib)
# --------------------------------------------------------------------------- #


def test_luck_finding_names_both_ends_with_signed_wins() -> None:
    from commishdesk.render import charts_static as cs

    assert cs.finding(_facts(), "luck") == (
        "Luckiest is Yardage Yaks at +2.5 wins; unluckiest is No-Huddle Narwhals at −1.8."
    )


def test_power_finding_names_the_top_team_and_the_biggest_mover() -> None:
    from commishdesk.render import charts_static as cs

    # Two Minute Drill is #1 and moved up two spots (a tie on |2| goes to the better-ranked team).
    assert cs.finding(_facts(), "power") == "#1 in the power rankings is Two Minute Drill, up 2 spots."

    raw = _raw()
    for team in raw["teams"]:
        if team["team_name"] == "Two Minute Drill":
            team["season"]["power"]["week_delta"] = 0
    other = cs.finding(WeeklyFacts.model_validate(raw), "power")
    assert other == "#1 is Two Minute Drill; the biggest move is No-Huddle Narwhals, down 2 spots."


def test_finding_truncates_a_long_name_like_the_web_chart() -> None:
    from commishdesk.render import charts_static as cs

    raw = _raw()
    long_name = "The Extraordinarily Long Team Name Of Doom"
    for team in raw["teams"]:
        if team["team_name"] == "Yardage Yaks":
            team["team_name"] = long_name
    facts = WeeklyFacts.model_validate(raw)
    assert long_name in cs.finding(facts, "luck")
    short = cs.finding(facts, "luck", name_max=24)
    assert "The Extraordinarily Lon…" in short
    assert long_name not in short


# --------------------------------------------------------------------------- #
# Real renders (need the charts extra)
# --------------------------------------------------------------------------- #


@needs_charts
def test_cards_are_1200_by_630_with_no_metadata() -> None:
    from commishdesk.render import charts_static as cs

    cards = cs.render_cards(_facts())
    assert list(cards) == ["luck", "power"]
    for card in cards.values():
        assert _size(card.png) == (1200, 630)
        assert "tEXt" not in _chunks(card.png) and "tIME" not in _chunks(card.png)
    assert cards["luck"].description == cs.finding(_facts(), "luck")
    assert "Trench Warfare" in cards["luck"].alt and "week 10" in cards["luck"].alt


@needs_charts
def test_card_bytes_and_manifest_are_deterministic() -> None:
    from commishdesk.render import charts_static as cs

    first = cs.render_cards(_facts(), url="commishdesk.com/l/x")
    second = cs.render_cards(_facts(), url="commishdesk.com/l/x")
    assert {k: v.png for k, v in first.items()} == {k: v.png for k, v in second.items()}
    assert {k: v.manifest_entry() for k, v in first.items()} == {k: v.manifest_entry() for k, v in second.items()}
    entry = first["luck"].manifest_entry()
    assert set(entry) == {"alt", "description", "bytes_sha256"} and len(entry["bytes_sha256"]) == 64
    assert first["luck"].png != first["power"].png


@needs_charts
def test_week_one_writes_no_cards() -> None:
    from commishdesk.render import charts_static as cs

    assert cs.render_cards(_facts("01")) == {}


@needs_charts
def test_long_team_names_render_and_the_finding_never_exceeds_four_lines() -> None:
    from commishdesk.render import charts_static as cs

    raw = _raw()
    for team in raw["teams"]:
        if team["team_name"] in {"Yardage Yaks", "No-Huddle Narwhals", "Two Minute Drill", "Flea Flicker Union"}:
            team["team_name"] = team["team_name"] + " Of The Extraordinarily Long Name Society"
    facts = WeeklyFacts.model_validate(raw)
    for panel in cs.PANELS:
        card = cs.social_card(facts, panel)
        assert _size(card.png) == (1200, 630)
        fig, _ax = cs.base_fig(1200, 630, background="black")
        lines = cs._wrapped_finding(fig, facts, panel, cs._font("display", 72), 1080)
        assert 1 <= len(lines) <= 4
        assert "Extraordinarily" in card.description  # alt/description keep the full name


@needs_charts
def test_a_card_that_raises_is_skipped_and_the_rest_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    from commishdesk.render import charts_static as cs

    real = cs.social_card

    def flaky(facts: WeeklyFacts, panel: Any, **kwargs: Any) -> Any:
        if panel == "luck":
            raise RuntimeError("boom")
        return real(facts, panel, **kwargs)

    monkeypatch.setattr(cs, "social_card", flaky)
    assert list(cs.render_cards(_facts())) == ["power"]


@needs_charts
def test_panel_pngs_render_for_every_team() -> None:
    from commishdesk.render import charts_static as cs

    for panel in cs.PANELS:
        png = cs.panel_png(_facts(), panel)
        width, height = _size(png)
        assert width == 1200 and height > 400
        assert cs.panel_png(_facts(), panel) == png


@needs_charts
def test_rendering_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from commishdesk.render import charts_static as cs

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network access during a static render")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    assert set(cs.render_cards(_facts())) == {"luck", "power"}


@needs_charts
def test_base_fig_is_an_agg_figure_with_no_pyplot_state() -> None:
    from commishdesk.render import charts_static as cs

    fig, ax = cs.base_fig(300, 200, background="black")
    assert type(fig.canvas).__name__ == "FigureCanvasAgg"
    assert (round(fig.get_figwidth() * fig.dpi), round(fig.get_figheight() * fig.dpi)) == (300, 200)
    assert ax.get_ylim() == (200, 0)
    assert "matplotlib.pyplot" not in sys.modules
