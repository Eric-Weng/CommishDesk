"""Story 5.14b — ``render_weekly_discord_post``, the designed weekly Discord post.

Every row of the spec's I/O matrix (week 10, with a URL, a 16-team league with
long names, a reissue, the published overlay, stood-down sections, hostile
names), plus the UTF-16 counting unit, the pure ``_trim`` order, and the
board's line order (``Discord.dc.html``).
"""

from __future__ import annotations

import json
import re
from typing import Any

from commishdesk.facts.schema import WeeklyFacts
from commishdesk.narrate.weekly_template import WeeklyIssue, WeeklySection, render_weekly_issue
from commishdesk.render import render_weekly_discord_post, utf16_len
from commishdesk.render.discord import LINK_RESERVE, WEEKLY_BODY_BUDGET, _Block, _join, _trim
from tests.conftest import REPO_ROOT

FACTS_DIR = REPO_ROOT / "tests" / "fixtures" / "facts"

#: The optional lines, first trimmed first (design-decisions "Discord rules").
_TRIM_MARKERS = ("🏆 Coach", "🔄 **Wire**", "👀 **Next week**", "🍀 **Luck**", "📈 **Power", "🏈 **Results**")


def _raw(published: bool = False) -> dict[str, Any]:
    name = "expected-weekly-facts-week10-published.json" if published else "expected-weekly-facts-week10.json"
    return json.loads((FACTS_DIR / name).read_text(encoding="utf-8"))


def _week01_raw() -> dict[str, Any]:
    return json.loads(
        (FACTS_DIR / "expected-weekly-facts-week01.json").read_text(encoding="utf-8")
    )


def _post(raw: dict[str, Any], issue: WeeklyIssue | None = None, **kwargs: Any) -> str:
    facts = WeeklyFacts.model_validate(raw)
    return render_weekly_discord_post(facts, issue or render_weekly_issue(facts.narration), **kwargs)


def _grow(raw: dict[str, Any], teams: int = 16, name_len: int = 30) -> dict[str, Any]:
    """Week 10 grown to *teams* rosters (two more games per four), every team
    name padded to *name_len* characters."""
    base = list(raw["teams"])
    for index in range(len(base), teams):
        clone = json.loads(json.dumps(base[index % len(base)]))
        clone["roster_id"] = str(index + 1)
        raw["teams"].append(clone)
        raw["standings"]["overall"].append(clone["roster_id"])
    for position, roster_id in enumerate(raw["standings"]["overall"], start=1):
        team = next(t for t in raw["teams"] if t["roster_id"] == roster_id)
        team["season"]["rank"] = position
    games = raw["matchups"]["this_week"]
    for index in range(len(base), teams, 2):
        game = json.loads(json.dumps(games[0]))
        game.update(home_roster_id=str(index + 1), away_roster_id=str(index + 2), winner_roster_id=str(index + 1),
                    is_blowout=False)
        games.append(game)
    raw["league"]["format"]["team_count"] = teams
    for team in raw["teams"]:
        stem = f"{team['team_name'] or 'Team'} {team['roster_id']} "
        team["team_name"] = (stem * 4)[:name_len].strip().ljust(name_len, "x")
    return raw


# --------------------------------------------------------------------------- #
# I/O matrix
# --------------------------------------------------------------------------- #


def test_week10_follows_the_boards_line_order_within_budget_and_no_link() -> None:
    post = _post(_raw())
    assert utf16_len(post) <= WEEKLY_BODY_BUDGET
    assert "Full Issue" not in post and not post.endswith("\n")
    lines = post.split("\n")
    assert lines[0] == "## 🏈 Trench Warfare · Week 10"
    assert lines[1] == (
        "**Kneel-Down Koalas lost by 6.06 with 108.86 on the bench, including TreVeyon Henderson.** 🪑"
    )
    assert lines[2] == "" and lines[3] == "📊 **Standings**" and lines[4] == "```"
    assert lines[5] == " 1 Two Minute Drill         9-1   2,029"  # the board's row, byte for byte
    assert lines[11] == "── playoff line ──────────────"
    assert lines[12] == " 7 Yardage Yaks             5-5   1,401"
    assert lines[17] == "12 Kneel-Down Koalas        2-8   1,437"
    assert lines[18] == "```"
    assert lines[19] == "🏈 **Results**"
    assert lines[20] == "• **Screen Pass Syndicate** 183.14 def. Bubble-Screen Bobcats 154.38"
    assert lines[23].endswith("247.20 def. Overtime Otters 114.69 🔥")
    assert lines[25].endswith("119.97 😬")
    assert lines[26] == ""
    assert lines[27] == (
        "📈 **Power top 5**  1. Two Minute Drill · 2. Flea Flicker Union · 3. No-Huddle Narwhals · "
        "4. Backpedal Buffalo · 5. Screen Pass Syndicate"
    )
    assert lines[28].startswith("🍀 **Luck**  luckiest Yardage Yaks (+2.5), unluckiest No-Huddle Narwhals (−1.8)")
    assert lines[29].startswith("👀 **Next week**  Game of the week: Backpedal Buffalo vs No-Huddle Narwhals")
    assert lines[30] == "🔄 **Wire**  Flea Flicker Union claimed Jalen Nailor ($25)"
    assert lines[31] == ""
    assert lines[32] == "🏆 Coach of the week: Two Minute Drill (99.2%)  ·  🥚 Goose egg: Dak Prescott (0.00)"
    assert len(lines) == 33


def test_with_a_url_the_link_line_comes_last_within_2000() -> None:
    post = _post(_raw(), issue_url="https://x/y")
    assert utf16_len(post) <= 2000
    assert post.endswith("\n\nFull Issue → https://x/y")
    assert _post(_raw(), issue_url=None) == post.rsplit("\n\n", 1)[0]


def test_sixteen_teams_with_long_names_trim_in_order_and_keep_the_core() -> None:
    raw = _grow(_raw())
    post = _post(raw)
    assert utf16_len(post) <= WEEKLY_BODY_BUDGET
    present = [marker in post for marker in _TRIM_MARKERS]
    assert not present[0], "the awards line goes first"
    # trimmed in order: once one optional line survives, every later one does
    assert present == sorted(present)
    lines = post.split("\n")
    assert lines[0].startswith("## 🏈 ") and lines[1].startswith("**")
    block = post.split("```\n", 1)[1].split("\n```", 1)[0].split("\n")
    assert [line[:2] for line in block[:6]] == [f"{n:>2}" for n in range(1, 7)]
    assert block[6] == "── playoff line ──────────────"
    # names in the block are capped at 22 characters with an ellipsis
    assert all(len(line) <= 2 + 1 + 22 + 3 + 5 + 3 + 5 for line in block if line[:2].strip().isdigit())
    assert "…" in block[0]


def test_sixteen_teams_with_a_url_stay_within_2000() -> None:
    post = _post(_grow(_raw()), issue_url="https://example.com/issues/trench-warfare/2025/week-10")
    assert utf16_len(post) <= 2000
    assert post.split("\n")[-1].startswith("Full Issue → ")


def test_a_reissue_puts_the_correction_under_the_title_and_never_trims_it() -> None:
    raw = _grow(_raw())
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    correction = "Correction — fixed the QB stat line — changed numbers: 204.52 -> 254.52"
    issue = issue.model_copy(
        update={"sections": [WeeklySection(heading="Correction", blocks=[correction]), *issue.sections]}
    )
    post = render_weekly_discord_post(facts, issue)
    assert utf16_len(post) <= WEEKLY_BODY_BUDGET
    lines = post.split("\n")
    assert lines[1] == correction.replace(">", "\\>")  # escaped like every league-derived value
    assert lines[2].startswith("**")


def test_an_oversized_markdown_heavy_correction_is_clipped_to_2000() -> None:
    raw = _grow(_raw())
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    correction = "Correction — " + "*bold* > quote _x_ " * 170  # ~3,000 characters, each escape grows it
    issue = issue.model_copy(
        update={"sections": [WeeklySection(heading="Correction", blocks=[correction]), *issue.sections]}
    )
    post = render_weekly_discord_post(facts, issue, issue_url="https://x/y")
    assert utf16_len(post) <= 2000
    assert post.endswith("…")


def test_with_no_hooked_lead_the_post_leads_with_the_narrated_lead() -> None:
    raw = _raw()
    raw["lead_candidates"] = []
    facts = WeeklyFacts.model_validate(raw)
    issue = render_weekly_issue(facts.narration)
    lead_blocks = next(s.blocks for s in issue.sections if s.heading == "The Lead")
    first = " ".join(lead_blocks[0].split())
    line = render_weekly_discord_post(facts, issue).split("\n")[1]
    assert line.startswith("**") and line.endswith("**") and "🪑" not in line
    assert line[2:40].replace("\\", "") == first[:38].replace("\\", "")


def test_power_top_five_follows_the_published_rank() -> None:
    raw = _raw(published=True)
    rows = {row["model_rank"]: row for row in raw["narration"]["power"]}
    rows[1]["published_rank"], rows[2]["published_rank"] = 2, 1
    post = _post(raw)
    power = next(line for line in post.split("\n") if line.startswith("📈"))
    assert power.startswith(f"📈 **Power top 5**  1. {rows[2]['team']} · 2. {rows[1]['team']} · ")


def test_stood_down_sections_drop_their_line_and_emoji() -> None:
    raw = _raw()
    for team in raw["teams"]:
        team["season"]["luck"] = None
    raw["transactions"]["this_week"] = []
    raw["transactions"]["recent_trades"] = []
    raw["matchups"]["next_week"] = []
    post = _post(raw)
    for gone in ("🍀", "🔄", "👀", "Luck", "Wire", "Next week"):
        assert gone not in post, gone
    assert "📈 **Power top 5**" in post and "🏈 **Results**" in post


def test_week01_cold_start_drops_power_luck_wire_and_playoff_line() -> None:
    """Story 5.16: at Week 1 the ticker drops the 📈 Power, 🍀 Luck and 🔄 Wire
    lines and never emits a playoff line or seeding note — matching the web page
    and the email — while the standings, results and next-week lines stay."""
    facts = WeeklyFacts.model_validate(_week01_raw())
    issue = render_weekly_issue(facts.narration)
    post = render_weekly_discord_post(facts, issue)

    assert "📈" not in post
    assert "🍀" not in post
    assert "🔄" not in post
    assert "── playoff line ──────────────" not in post
    assert "Seeding unconfirmed" not in post

    assert "📊 **Standings**" in post
    assert "🏈 **Results**" in post
    assert "👀 **Next week**" in post


def test_the_wire_falls_back_to_the_newest_trade() -> None:
    raw = _raw()
    raw["transactions"]["this_week"] = []
    wire = next(line for line in _post(raw).split("\n") if line.startswith("🔄"))
    assert wire.startswith("🔄 **Wire**  Week 9 trade: ")
    assert "Tyler Allgeier" in wire and "a 2026 round 4 pick" in wire


def test_hostile_names_are_inert_in_text_and_code_block() -> None:
    raw = _raw()
    html_name = '<script>x</script>"><svg onload=1>'
    md_name = "a`b*c@everyone"
    for team in raw["teams"]:
        if team["roster_id"] == "2":  # standings #1, power #1, coach of the week
            team["team_name"] = md_name
        if team["roster_id"] == "12":  # the closest game's winner, luckiest
            team["team_name"] = html_name
    post = _post(raw)
    assert post.count("```") == 2  # a backtick in a name never opens or closes a fence
    block = post.split("```\n", 1)[1].split("\n```", 1)[0]
    outside = post.replace(block, "")
    assert " 1 a'b*c@everyone" in block  # unescaped, backtick replaced
    assert "a\\`b\\*c@everyone" in outside  # escaped outside the block
    assert md_name not in outside
    assert '<script\\>x</script\\>"\\>' in outside  # plain text on Discord; the ">" markdown is escaped
    assert html_name not in outside
    assert "<script>x</script>\"><…" in block  # capped at 22 characters (21 + the ellipsis)


def test_a_team_name_cannot_post_a_masked_link() -> None:
    raw = _raw()
    for team in raw["teams"]:
        if team["roster_id"] == "2":  # power #1, coach of the week
            team["team_name"] = "[Free Nitro](https://evil.example)"
    post = _post(raw)
    block = post.split("```\n", 1)[1].split("\n```", 1)[0]
    outside = post.replace(block, "")
    assert "\\[Free Nitro](https://evil.example)" in outside
    assert "[Free Nitro](" not in outside.replace("\\[Free Nitro](", "")


def test_the_render_is_deterministic() -> None:
    assert _post(_raw(published=True)) == _post(_raw(published=True))


# --------------------------------------------------------------------------- #
# Units and the trim helper
# --------------------------------------------------------------------------- #


def test_every_emoji_counts_two_utf16_units() -> None:
    for emoji in ("🏈", "📊", "📈", "🍀", "👀", "🔄", "🏆", "🥚", "🔥", "😬", "🪑"):
        assert utf16_len(emoji) == 2, emoji
    assert utf16_len("abc") == 3 and utf16_len("→·—") == 3
    assert LINK_RESERVE == 60 and WEEKLY_BODY_BUDGET == 1940


def test_trim_drops_lowest_priority_first_and_keeps_order() -> None:
    blocks = [
        _Block("title", 0),
        _Block("row7", 1, 101),
        _Block("row8", 1, 100),
        _Block("results", 1, 6),
        _Block("power", 2, 5),
        _Block("wire", 2, 2),
        _Block("awards", 3, 1),
    ]
    full = _join(blocks)
    assert full == "title\n\nrow7\nrow8\nresults\n\npower\nwire\n\nawards"
    assert _trim(blocks, len(full)) == blocks
    assert [b.text for b in _trim(blocks, len(full) - 1)] == ["title", "row7", "row8", "results", "power", "wire"]
    assert [b.text for b in _trim(blocks, 20)] == ["title", "row7", "row8"]
    assert [b.text for b in _trim(blocks, 12)] == ["title", "row7"]  # the bottom row goes before row 7
    assert [b.text for b in _trim(blocks, 0)] == ["title"]  # the core is never trimmed


def test_no_link_line_leaves_no_trailing_blank_line() -> None:
    post = _post(_raw())
    assert not re.search(r"\n\s*$", post)


def test_the_unconfirmed_seeding_note_follows_the_facts_flag_alone() -> None:
    note = "⚠️ Seeding unconfirmed — derived from the standings"
    raw = _raw()
    assert raw["standings"]["playoff_picture"]["seeding_unconfirmed"] is False
    base_issue = render_weekly_issue(WeeklyFacts.model_validate(raw).narration)
    off = _post(raw, base_issue)
    assert "Seeding unconfirmed" not in off

    flagged = json.loads(json.dumps(raw))
    flagged["standings"]["playoff_picture"]["seeding_unconfirmed"] = True
    on = _post(flagged, base_issue)
    assert note in on


def test_week01_standings_rows_are_ordered_by_points_for() -> None:
    raw = _week01_raw()
    expected = [
        t["team_name"] for t in sorted(raw["teams"], key=lambda t: -t["season"]["points_for"])
    ]
    post = _post(raw)
    block = post.split("📊 **Standings**", 1)[1].split("🏈 **Results**", 1)[0]
    positions = [block.index(name) for name in expected]
    assert positions == sorted(positions)


def test_warm_week_keeps_its_power_line() -> None:
    post = _post(_raw())
    assert "📈 **Power top 5**" in post


def test_week01_standings_ranks_are_positional() -> None:
    post = _post(_week01_raw())
    block = post.split("📊 **Standings**", 1)[1].split("🏈 **Results**", 1)[0]
    rows = [line for line in block.splitlines() if line and not line.startswith("`")]
    assert [int(line.split()[0]) for line in rows] == list(range(1, 13))
