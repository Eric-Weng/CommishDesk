"""Regenerate the League Value fixtures (Story 9.6a): ``python tests/fixtures/league_value/build_fixtures.py``.

Everything is synthetic: invented ids and names, stat lines from a fixed formula. The CSV files carry the real
nflverse column header (so the parser meets the real shape) with zeros in unused columns. The tests do not import
this file; they read the committed outputs and recompute the numbers with their own oracle.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).parent
SEASON = 2026
WEEKS = 4

# The real stats_player_week header (nflverse-data release `stats_player`), as of 2026-10.
HEADER = (
    "player_id,player_name,player_display_name,position,position_group,headshot_url,season,week,season_type,game_id,"
    "team,opponent_team,completions,attempts,passing_yards,passing_tds,passing_interceptions,sacks_suffered,"
    "sack_yards_lost,sack_fumbles,sack_fumbles_lost,passing_air_yards,passing_yards_after_catch,passing_first_downs,"
    "passing_epa,passing_cpoe,passing_2pt_conversions,pacr,passing_10,passing_16,passing_20,passing_40,carries,"
    "rushing_yards,rushing_tds,rushing_fumbles,rushing_fumbles_lost,rushing_first_downs,rushing_epa,"
    "rushing_2pt_conversions,rushing_10,rushing_12,rushing_20,rushing_40,receptions,targets,receiving_yards,"
    "receiving_tds,receiving_fumbles,receiving_fumbles_lost,receiving_air_yards,receiving_yards_after_catch,"
    "receiving_first_downs,receiving_epa,receiving_2pt_conversions,receiving_10,receiving_16,receiving_20,"
    "receiving_40,racr,target_share,air_yards_share,wopr,special_teams_tds,def_tackles_solo,fantasy_points,"
    "fantasy_points_ppr,punt_return_yards,kickoff_return_yards"
).split(",")

PLAYERS_HEADER = (
    "gsis_id,display_name,common_first_name,first_name,last_name,short_name,football_name,suffix,esb_id,nfl_id,"
    "pfr_id,pff_id,otc_id,espn_id,smart_id,birth_date,position_group,position,ngs_position_group,ngs_position,"
    "height,weight,headshot,college_name,college_conference,jersey_number,rookie_season,last_season,latest_team,"
    "status,ngs_status,ngs_status_short_description,years_of_experience,pff_position,pff_status,draft_year,"
    "draft_round,draft_pick,draft_team"
).split(",")

SCORING = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -2, "rush_yd": 0.1, "rush_td": 6, "rec": 1, "rec_yd": 0.1,
    "rec_td": 6, "fum_lost": -2, "bonus_rec_te": 0.5, "bonus_rush_yd_100": 2,
    "rec_40p": 1, "rush_40p": 1, "pass_td_40p": 1,
    "fgm_0_19": 3, "xpm": 1, "pts_allow_0": 10,
}  # fmt: skip

COUNTS = (("QB", 4), ("RB", 8), ("WR", 8), ("TE", 4))
LEVEL = {"QB": [8, 7, 6, 3], "RB": [9, 8, 7, 6, 5, 4, 3, 2], "WR": [9, 8, 7, 6, 5, 4, 3, 2], "TE": [7, 5, 3, 2]}
# How the Sleeper record joins to nflverse. Default "gsis".
PATH = {("QB", 1): "espn", ("QB", 2): "name_birth", ("RB", 1): "name_pos", ("RB", 7): "none", ("WR", 7): "none"}
FROM_WEEK = {("WR", 2): 3}  # rostered in the league from week 3 only; weeks 1-2 come from nflverse
GAP_BONUS = {("WR", 0): {1: 1.5}}  # the league's own number differs from nflverse scoring (a play-length bonus)
PRIOR_GAMES = {("RB", 2): 3}  # 3 prior games: the prior does not qualify (it needs 4)
PREFIX = {"QB": "Quinn", "RB": "Rudy", "WR": "Wade", "TE": "Teddy"}
SURNAMES = ["Alder", "Birch", "Cedar", "Dale", "Elm", "Fir", "Gorse", "Holly"]
BASE_ID = {"QB": 100, "RB": 200, "WR": 300, "TE": 400}


def line(pos: str, level: int, week: int) -> dict[str, float]:
    if pos == "QB":
        return {"passing_yards": 150 + 25 * level, "passing_tds": level // 3, "passing_interceptions": week % 2,
                "carries": 4, "rushing_yards": 10 + level}  # fmt: skip
    if pos == "RB":
        return {"carries": 8 + 2 * level, "rushing_yards": 30 + 9 * level + 3 * week,
                "rushing_tds": 1 if (week + level) % 3 == 0 else 0, "receptions": level // 2,
                "receiving_yards": 4 * level,
                "rushing_fumbles_lost": 1 if (week == 2 and level == 9) else 0}  # fmt: skip
    if pos == "WR":
        return {"receptions": 2 + level // 2, "targets": 4 + level // 2, "receiving_yards": 20 + 8 * level + week,
                "receiving_tds": 1 if (week * level) % 7 == 0 else 0}  # fmt: skip
    return {"receptions": 1 + level // 2, "targets": 3 + level // 2, "receiving_yards": 10 + 6 * level + week,
            "receiving_tds": 1 if (week + level) % 5 == 0 else 0}  # fmt: skip


def oracle(stats: dict[str, float], pos: str) -> float:
    """The league's scoring applied by hand (independent of the engine)."""
    g = stats.get
    pts = 0.04 * g("passing_yards", 0) + 4 * g("passing_tds", 0) - 2 * g("passing_interceptions", 0)
    pts += 0.1 * g("rushing_yards", 0) + 6 * g("rushing_tds", 0)
    pts += g("receptions", 0) + 0.1 * g("receiving_yards", 0) + 6 * g("receiving_tds", 0)
    pts -= 2 * g("rushing_fumbles_lost", 0)
    if pos == "TE":
        pts += 0.5 * g("receptions", 0)
    if g("rushing_yards", 0) >= 100:
        pts += 2
    return round(pts, 6)


def stat_row(gsis: str, name: str, pos: str, season: int, week: int, stats: dict[str, float]) -> dict[str, str]:
    row = dict.fromkeys(HEADER, "0")
    row.update(player_id=gsis, player_name=name, player_display_name=name, position=pos, position_group=pos,
               season=str(season), week=str(week), season_type="REG", game_id=f"{season}_{week:02d}", team="XXX",
               opponent_team="YYY", headshot_url="", fantasy_points="", fantasy_points_ppr="")  # fmt: skip
    row.update({k: str(v) for k, v in stats.items()})
    return row


def main() -> None:
    players: dict[str, dict] = {}
    pp: dict[int, dict[str, float]] = {w: {} for w in range(1, WEEKS + 1)}
    cur_rows, prior_rows, nv_players = [], [], []
    ordinal = 0
    for pos, count in COUNTS:
        for i in range(count):
            ordinal += 1
            pid, gsis = str(BASE_ID[pos] + i), f"00-00{BASE_ID[pos] + i}"
            name = f"{PREFIX[pos]} {SURNAMES[i]}"
            level, path = LEVEL[pos][i], PATH.get((pos, i), "gsis")
            birth = f"{1996 + i % 4}-0{i % 9 + 1}-1{i}"
            espn = str(5000 + BASE_ID[pos] + i)
            rec = {"position": pos, "first_name": name.split()[0], "last_name": name.split()[1], "full_name": name,
                   "years_exp": 3, "birth_date": birth, "search_rank": 10 * ordinal,
                   "fantasy_positions": [pos]}  # fmt: skip
            nv_espn, nv_birth = "", birth
            if path == "gsis":
                rec.update(gsis_id=gsis, espn_id=espn)
                nv_espn = espn
            elif path == "espn":
                rec.update(espn_id=espn)
                nv_espn = espn
            elif path == "name_pos":
                rec["birth_date"] = None
                nv_birth = ""
            elif path == "none":
                rec.update(gsis_id=f"00-99{BASE_ID[pos] + i}", full_name=f"{name} Nine")
            players[pid] = rec
            if path != "none":
                nv_players.append([gsis, nv_espn, nv_birth, name, pos])
                for wk in range(1, WEEKS + 1):
                    cur_rows.append(stat_row(gsis, name, pos, SEASON, wk, line(pos, level, wk)))
                for wk in range(1, PRIOR_GAMES.get((pos, i), 17) + 1):
                    prior_rows.append(stat_row(gsis, name, pos, SEASON - 1, wk, line(pos, level, wk + 1)))
            for wk in range(FROM_WEEK.get((pos, i), 1), WEEKS + 1):
                bonus = GAP_BONUS.get((pos, i), {}).get(wk, 0.0)
                pp[wk][pid] = round(oracle(line(pos, level, wk), pos) + bonus, 6)

    # Rookies, no nflverse history. 900: one game. 901: no games. 902: three games (established).
    for pos, pid, name, rank, games in [
        ("RB", "900", "Rookie Ash", 55, {4: 9.0}),
        ("WR", "901", "Rookie Beech", 92, {}),
        ("WR", "902", "Rookie Cypress", 130, {2: 8.0, 3: 10.5, 4: 7.5}),
    ]:
        players[pid] = {"position": pos, "first_name": name.split()[0], "last_name": name.split()[1],
                        "full_name": name, "years_exp": 0, "birth_date": "2004-05-05", "search_rank": rank,
                        "fantasy_positions": [pos]}  # fmt: skip
        for wk in range(1, WEEKS + 1):
            pp[wk][pid] = games.get(wk, 0.0)

    # K / DEF: players_points only.
    kd = [("K", "700", [8, 9, 7, 10]), ("K", "701", [6, 7, 6, 8]), ("K", "702", [5, 6, 4, 5]),
          ("DEF", "NYA", [6, 12, 3, 9]), ("DEF", "NYB", [4, 5, 7, 6]), ("DEF", "NYC", [2, 3, 3, 2])]  # fmt: skip
    for j, (pos, pid, pts) in enumerate(kd):
        players[pid] = {"position": pos, "full_name": f"{pos} unit {j}", "years_exp": 5, "search_rank": 400 + j}
        for wk in range(1, WEEKS + 1):
            pp[wk][pid] = float(pts[wk - 1])
    # A kicker nobody scored with: no points anywhere, so unvalued (never zero).
    players["703"] = {"position": "K", "full_name": "K unit idle", "years_exp": 5}

    league = {
        "league_id": "9001", "season": str(SEASON), "scoring_settings": SCORING,
        "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"],
        "settings": {"num_teams": 3, "type": 0, "draft_rounds": 2},
    }  # fmt: skip
    matchups = {str(wk): [{"roster_id": 1, "matchup_id": 1, "players_points": pp[wk]}] for wk in range(1, WEEKS + 1)}
    bundle = {"league": league, "matchups": matchups, "players": players, "standings_order": ["2", "1", "3"]}
    (HERE / "league.json").write_text(json.dumps(bundle, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def write(name: str, header: list[str], rows: list[dict[str, str]]) -> None:
        with (HERE / name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    write(f"stats_player_week_{SEASON}.csv", HEADER, cur_rows)
    write(f"stats_player_week_{SEASON - 1}.csv", HEADER, prior_rows)
    prow = []
    for gsis, espn, birth, name, pos in nv_players:
        row = dict.fromkeys(PLAYERS_HEADER, "")
        row.update(gsis_id=gsis, display_name=name, espn_id=espn, birth_date=birth, position=pos, position_group=pos,
                   last_season=str(SEASON), first_name=name.split()[0], last_name=name.split()[1])  # fmt: skip
        prow.append(row)
    write("players.csv", PLAYERS_HEADER, prow)


if __name__ == "__main__":
    main()
