"""Monte Carlo playoff odds (Story 9.4): the public reference stat module.

Pure standard library. Reads only the Facts it is handed and plays the rest of the regular
season 10,000 times with a seeded generator, so a re-issue is byte-identical. Odds are integer
counts over :data:`SIMULATIONS`. Clinched and eliminated teams (the flags on the next-week
cards, the same ones ``stats/stakes.py`` reads) are held in or out inside every simulated
season, so the odds can never contradict them and playoff-entry odds sum to the bracket size.
The method is this module's own (AD-23); ``docs/concepts/playoff-odds.md`` states it in words.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from typing import Any

from commishdesk.facts import FactsJSON

__all__ = ["SIMULATIONS", "STAT_MODULE", "PlayoffOddsModule"]

SIMULATIONS = 10_000
PRIOR_GAMES = 3.0
"""How many games of league-average scoring each team's own average is blended with."""

_IN, _OUT, _OPEN = 1, -1, 0
_FLAG_NAMES = ("clinched_playoff", "clinched_bye", "eliminated", "clinched_division")


def _roster_key(roster_id: str) -> tuple[int, int, str]:
    return (0, int(roster_id), roster_id) if roster_id.isdigit() else (1, 0, roster_id)


def _seed(league_id: str, week: int, version: str) -> int:
    digest = hashlib.sha256(f"{league_id}|{week}|{version}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _team_models(
    histories: Mapping[str, Sequence[float]],
) -> tuple[dict[str, float], float]:
    """Each team's expected score and the one league-pooled spread.

    A team's mean is pulled toward the league mean as if it had also played ``PRIOR_GAMES``
    games at the league average, so a hot week or two does not run away with the odds."""
    scores = [x for xs in histories.values() for x in xs]
    league_mean = sum(scores) / len(scores)
    squares, dof = 0.0, 0
    for xs in histories.values():
        if len(xs) > 1:
            mean = sum(xs) / len(xs)
            squares += sum((x - mean) ** 2 for x in xs)
            dof += len(xs) - 1
    if dof:
        spread = math.sqrt(squares / dof)
    else:  # one game each: the spread across teams is the only evidence there is
        spread = math.sqrt(sum((x - league_mean) ** 2 for x in scores) / len(scores))
    if spread <= 0:
        spread = max(1.0, 0.2 * abs(league_mean))
    means = {
        rid: (sum(xs) + PRIOR_GAMES * league_mean) / (len(xs) + PRIOR_GAMES) if xs else league_mean
        for rid, xs in histories.items()
    }
    return means, spread


def _flags(facts: Mapping[str, Any]) -> dict[str, dict[str, bool]]:
    found: dict[str, dict[str, bool]] = {}
    for card in facts["matchups"]["next_week"]:
        for side in ("a", "b"):
            found[str(card[f"{side}_roster_id"])] = {
                name: bool(card.get(f"{side}_{name}")) for name in _FLAG_NAMES
            }
    return found


def _status(flags: Mapping[str, bool]) -> str:
    if flags.get("clinched_playoff") or flags.get("clinched_bye"):
        return "clinched"
    return "eliminated" if flags.get("eliminated") else "in_race"


def _odds(counts: int) -> float:
    return round(counts / SIMULATIONS, 4)


def _compute(facts: FactsJSON, version: str) -> dict[str, Any]:
    playoff = facts["league"]["format"].get("playoff")
    standings = facts["standings"]
    remaining = facts["matchups"]["remaining"]
    if not playoff or standings.get("regular_season_complete") or not remaining.get("complete"):
        return {}
    teams = {str(t["roster_id"]): t for t in facts["teams"]}
    if not teams:
        return {}
    rec = {rid: t["season"]["record"] for rid, t in teams.items()}
    if sum(r["w"] + r["l"] + r["t"] for r in rec.values()) == 0:
        return {}

    ids = sorted(teams, key=_roster_key)
    index = {rid: i for i, rid in enumerate(ids)}
    n = len(ids)
    histories = {
        rid: [float(g["points"]) for g in t["history"]["weekly"] if isinstance(g.get("points"), int | float)]
        for rid, t in teams.items()
    }
    if not any(histories.values()):
        return {}
    means, spread = _team_models(histories)
    mu = [means[rid] for rid in ids]
    games = [
        (index[str(g["a_roster_id"])], index[str(g["b_roster_id"])])
        for wk in sorted(remaining["weeks"], key=lambda w: w["week"])
        for g in wk["games"]
        if str(g["a_roster_id"]) in index and str(g["b_roster_id"]) in index
    ]

    flags = _flags(facts)
    picture = standings.get("playoff_picture") or {}
    commissioner = picture.get("source") == "commissioner"
    bracket, byes = int(playoff["bracket_teams"]), int(playoff["byes"])
    declared = bool(facts["league"]["format"].get("divisions"))

    def prio(name: str, rid: str) -> int:
        f = flags.get(rid, {})
        if name == "entry":
            return _IN if f.get("clinched_playoff") or f.get("clinched_bye") else _OUT if f.get("eliminated") else _OPEN
        if name == "bye":
            return _IN if f.get("clinched_bye") else _OUT if f.get("eliminated") else _OPEN
        return _IN if f.get("clinched_division") else _OUT if f.get("eliminated") else _OPEN

    entry_prio = [prio("entry", rid) for rid in ids]
    bye_prio = [prio("bye", rid) for rid in ids]
    div_prio = [prio("division", rid) for rid in ids]
    fixed_in = {index[r] for r in picture.get("in_bracket", []) if r in index}
    fixed_bye = {index[r] for r in picture.get("byes", []) if r in index}
    division_of = [teams[rid].get("division_id") if declared else None for rid in ids]
    groups: dict[Any, list[int]] = {}
    for i, d in enumerate(division_of):
        if d is not None:
            groups.setdefault(d, []).append(i)

    base_w = [2 * rec[rid]["w"] + rec[rid]["t"] for rid in ids]
    base_pf = [float(teams[rid]["season"]["points_for"]) for rid in ids]
    made = [0] * n
    got_bye = [0] * n
    won_div = [0] * n
    rng = random.Random(_seed(str(facts["league"]["id"]), int(facts["week"]), version))
    gauss = rng.gauss

    for _ in range(SIMULATIONS):
        w, pf = base_w[:], base_pf[:]
        for a, b in games:
            sa, sb = gauss(mu[a], spread), gauss(mu[b], spread)
            pf[a] += sa
            pf[b] += sb
            w[a if sa > sb else b] += 2
        if commissioner:
            entrants = sorted(fixed_in)
        else:
            order = sorted(range(n), key=lambda i: (entry_prio[i], w[i], pf[i]), reverse=True)
            entrants = order[:bracket]
        for i in entrants:
            made[i] += 1
        if byes > 0:
            if commissioner:
                seeded = sorted(fixed_bye)
            else:
                seeded = sorted(entrants, key=lambda i: (bye_prio[i], w[i], pf[i]), reverse=True)[:byes]
            for i in seeded:
                got_bye[i] += 1
        for members in groups.values():
            won_div[max(members, key=lambda i: (div_prio[i], w[i], pf[i]))] += 1

    rows = []
    for rid in ids:
        i = index[rid]
        status = _status(flags.get(rid, {}))
        if commissioner:
            status = "clinched" if i in fixed_in else "eliminated"
        row: dict[str, Any] = {"roster_id": rid, "make_playoffs": _odds(made[i]), "status": status}
        if byes > 0:
            row["bye"] = _odds(got_bye[i])
        if division_of[i] is not None:
            row["division"] = _odds(won_div[i])
        rows.append(row)
    return {
        "simulations": SIMULATIONS,
        "module_version": version,
        "through_week": int(standings["through_week"]),
        "teams": rows,
    }


class PlayoffOddsModule:
    module_id = "playoff_odds"
    module_version = "1"

    def compute(self, facts: FactsJSON) -> Mapping[str, object]:
        block = _compute(facts, self.module_version)
        return {self.module_id: block} if block else {}


STAT_MODULE = PlayoffOddsModule()
