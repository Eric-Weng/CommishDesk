#!/usr/bin/env python3
"""Anonymize one raw Sleeper league bundle into a committable fixture.

This module is **not** part of the installed ``commishdesk`` package. It ships in
``tools/`` (no ``__init__.py``) so the build backend never packages it, and it
depends only on the standard library plus ``pydantic`` v2 — no new runtime
dependency.

What it does, given one JSON object in the *bundle shape*
(see ``tests/fixtures/README.md``):

* validates the top-level structure and **rejects** — before touching any data —
  a bundle with an unknown top-level key, a missing required section, or a
  wrong-typed section;
* strips every section, and every ``metadata`` sub-object, to an allowlist of
  structural / enum fields — owner-authored free text (trade notes, draft
  descriptions, custom player nicknames, division labels, trophy banners) is
  dropped, not passed through;
* replaces member handles / team names with entries from :data:`NAME_POOL`;
* rewrites every Sleeper account / league / draft id to a short opaque token
  (``id_`` + 10 chars), regardless of the id's length; one real id maps to
  exactly one token for a given ``--seed``, and that mapping does not depend on
  which slice of the league is being anonymized — so fixtures cut from different
  weeks of one league agree on the token for a given team;
* drops avatar hashes and any ``sleepercdn.com`` URL;
* remaps every ``created`` / ``status_updated`` epoch-ms onto a synthetic grid
  (:data:`SYNTH_EPOCH_BASE_MS` + rank x :data:`SYNTH_EPOCH_STEP_MS` over the
  sorted distinct set), order-preserving — a real transaction wall-clock is a
  strong fingerprint of a private league;
* trims each NFL player record to the :data:`_PLAYER_FIELDS` allowlist. The raw
  record carries birth date, birth city, high school, third-party ids, … — none
  used by the engine and none anybody's to publish. ``college`` / ``age`` /
  ``rookie_year`` *are* kept: published on every NFL roster site, and what a
  draft recap cites;
* preserves verbatim: ``scoring_settings``, ``roster_positions``, ``settings``,
  matchup points / players / starters / matchup_id, and transaction / draft /
  bracket structure.

It does **not** assemble the bundle from a per-endpoint export, do week-window
truncation, drop failed waiver claims, or apply the superflex roster-slot
mutation — that is ``tools/assemble_bundle.py``, run first (see
``tests/fixtures/README.md``).

Usage::

    python tools/assemble_bundle.py raw-dir <case> | python tools/anonymize.py - > fixture.json
    python tools/anonymize.py path/to/bundle.json --seed 0 > fixture.json
    cat bundle.json | python tools/anonymize.py - > fixture.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import string
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "NAME_POOL",
    "Bundle",
    "load_bundle",
    "anonymize_bundle",
    "main",
    "SYNTH_EPOCH_BASE_MS",
    "SYNTH_EPOCH_STEP_MS",
]

# --------------------------------------------------------------------------- #
# Generated-name pool
# --------------------------------------------------------------------------- #
# Used for BOTH ``display_name`` and ``team_name``. ``tests/test_fixtures.py``
# asserts positively that every name in every fixture is a member of this pool —
# a real-name *denylist* would itself leak the names it is meant to hide. Every
# entry is distinct; a league needs ``2 * members + 1`` of them.
NAME_POOL: tuple[str, ...] = (
    "Gridiron Aardvarks",
    "Blitz Alpacas",
    "Audible Armadillos",
    "Backfield Badgers",
    "Bootleg Bandicoots",
    "Blueline Bison",
    "Bubble-Screen Bobcats",
    "Backpedal Buffalo",
    "Coverage Capybaras",
    "Checkdown Caribou",
    "Counter-Trey Cheetahs",
    "Chip-Block Chinchillas",
    "Cover-Two Cobras",
    "Clock-Kill Condors",
    "Cadence Cougars",
    "Cutback Coyotes",
    "Coffin-Corner Cranes",
    "Dime-Package Dingoes",
    "Draw-Play Dromedaries",
    "End-Around Egrets",
    "Extra-Point Elk",
    "Flea-Flicker Falcons",
    "Fair-Catch Ferrets",
    "Field-Goal Foxes",
    "Goal-Line Gazelles",
    "Gap-Scheme Geckos",
    "Gridiron Gophers",
    "Gunner Grizzlies",
    "Hash-Mark Herons",
    "Iron-Range Ibex",
    "Icing Iguanas",
    "Jet-Sweep Jackals",
    "Juke-Move Jaguars",
    "Keeper Kestrels",
    "Kneel-Down Koalas",
    "Kickoff Kudus",
    "Lead-Block Lemurs",
    "Long-Snap Lynx",
    "Midfield Magpies",
    "Motion Mallards",
    "Two-Minute Marmots",
    "Man-Coverage Meerkats",
    "Muffed-Punt Moose",
    "No-Huddle Narwhals",
    "Nickel Newts",
    "Onside Ocelots",
    "Option-Route Ospreys",
    "Overtime Otters",
    "Play-Action Pandas",
    "Pocket Pelicans",
    "Pull-Guard Pumas",
    "Quarterback Quails",
    "Red-Zone Ravens",
    "Rollout Rhinos",
    "Route-Tree Roadrunners",
    "Sack-Dance Salamanders",
    "Sideline Sasquatch",
    "Screen-Pass Seagulls",
    "Spike Sparrows",
    "Stiff-Arm Stags",
    "Slot-Fade Storks",
    "Trap-Block Tapirs",
    "Tight-End Terriers",
    "Trench Tortoises",
    "Trick-Play Turtles",
    "Up-Tempo Urchins",
    "Victory-Formation Vipers",
    "Veer-Option Voles",
    "Wildcat Walruses",
    "Wrap-Tackle Weasels",
    "Wide-Nine Wolverines",
    "Wishbone Wombats",
    "Yardage Yaks",
    "Zone-Blitz Zebras",
    "Punt Formation",
    "Hidden Yardage",
    "Flea Flicker Union",
    "Screen Pass Syndicate",
    "Victory Formation",
    "Halfback Hollow",
    "Two Minute Drill",
    "Trench Warfare",
    "Prevent Defense",
    "Hail Mary Club",
    "Chain Gang",
    "Pylon Cam",
    "Coin Toss",
    "Neutral Zone",
    "Delay of Game",
    "Onside Gamble",
    "Hurry-Up Offense",
    "Kill the Clock",
)

# --------------------------------------------------------------------------- #
# Structural validation
# --------------------------------------------------------------------------- #


class Bundle(BaseModel):
    """The bundle shape, typed loosely on purpose.

    ``extra="forbid"`` on the top level is the whole point: an unknown top-level
    key is rejected here, before :func:`anonymize_bundle` runs. Sections are typed
    only as "an object" or "an array" — the nested Sleeper payloads pass through
    and are handled field-by-field by the anonymizer's allowlists.
    """

    model_config = ConfigDict(extra="forbid")

    meta: dict[str, Any]
    league: dict[str, Any]
    users: list[Any]
    rosters: list[Any]
    matchups: dict[str, Any] = Field(default_factory=dict)
    transactions: dict[str, Any] = Field(default_factory=dict)
    draft: dict[str, Any] | None = None
    draft_picks: list[Any] = Field(default_factory=list)
    traded_picks: list[Any] = Field(default_factory=list)
    winners_bracket: list[Any] = Field(default_factory=list)
    losers_bracket: list[Any] = Field(default_factory=list)
    players: dict[str, Any] = Field(default_factory=dict)


Bundle.model_rebuild()


def load_bundle(source: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Parse and structurally validate a bundle.

    Accepts a JSON string / bytes or an already-parsed object. Raises
    ``json.JSONDecodeError`` on malformed JSON and ``pydantic.ValidationError``
    on a structural problem (unknown top-level key, missing or wrong-typed
    section). Returns the validated bundle as a plain ``dict``.
    """
    obj = source if isinstance(source, dict) else json.loads(source)
    return Bundle.model_validate(obj).model_dump()


# --------------------------------------------------------------------------- #
# Anonymization
# --------------------------------------------------------------------------- #

_TOKEN_ALPHABET = string.ascii_lowercase + string.digits
_DIGIT_RUN_15 = re.compile(r"\d{15,}")

# Synthetic timestamp grid. Every ``created`` / ``status_updated`` epoch-ms in the
# anonymized bundle is remapped, order-preserving, onto ``BASE + i * STEP`` where
# ``i`` is the value's rank in the sorted distinct set. A real Sleeper transaction
# timestamp is a strong fingerprint of a private league (Sleeper is readable by
# ``league_id`` without credentials); the grid keeps "X happened before Y" but
# drops the wall-clock. Deterministic and independent of ``--seed``.
SYNTH_EPOCH_BASE_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
SYNTH_EPOCH_STEP_MS = 3_600_000  # 1 hour
_TIMESTAMP_KEYS = frozenset({"created", "status_updated"})


def _coerce_epoch(value: Any) -> int | None:
    """A positive wall-clock as an int, or ``None`` if ``value`` is not one.

    Sleeper serves ``created`` / ``status_updated`` as ints, but a contributor's
    export (or schema drift) could carry a numeric string or a whole float — the
    remap normalises all of them so none slips past as an un-gridded original.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 and value.is_integer() else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None

# Scalar fields whose value is a Sleeper account / league / draft id. Handled
# explicitly by the section builders and again, defensively, by ``_sweep``.
_SCALAR_ID_KEYS = frozenset(
    {
        "user_id",
        "owner_id",
        "league_id",
        "draft_id",
        "previous_league_id",
        "copy_from_league_id",
        "creator",
        "picked_by",
        "last_author_id",
    }
)
# List fields whose items are Sleeper account ids.
_LIST_ID_KEYS = frozenset({"co_owners", "creators"})

# NFL player record: the only fields the engine consumes. Everything else in the
# raw record (birth_date, birth_city, high_school, third-party ids, news
# timestamps, …) is dropped — a size win and a privacy boundary. ``college`` /
# ``age`` / ``rookie_year`` are published on every NFL roster site (not
# league-member data) and are what a draft recap cites to tell a rookie from a
# veteran. ``rookie_year`` lives in the raw record's ``metadata`` sub-object, not
# at the top level — ``players()`` lifts it.
_PLAYER_FIELDS = (
    "first_name",
    "last_name",
    "position",
    "team",
    "years_exp",
    "number",
    "injury_status",
    "fantasy_positions",
    "status",
    "college",
    "age",
    "rookie_year",
)
# Player fields that come from ``rec["metadata"]`` rather than the top level.
_PLAYER_META_FIELDS = frozenset({"rookie_year"})

_MATCHUP_FIELDS = (
    "roster_id",
    "matchup_id",
    "points",
    "custom_points",
    "players",
    "starters",
)

_DRAFT_PICK_META_FIELDS = (
    "first_name",
    "last_name",
    "position",
    "team",
    "number",
    "years_exp",
    "injury_status",
    "status",
    "player_id",
)

# ``league.metadata`` — keep only structural / enum values. Division *labels*,
# trophy *banner text*, and anything else owner-authored is dropped.
_LEAGUE_META_KEYS = frozenset(
    {
        "continued",
        "keeper_deadline",
        "latest_league_winner_roster_id",
        "trophy_loser",
        "trophy_winner",
        "trophy_loser_background",
        "trophy_winner_background",
    }
)
_ROSTER_META_KEYS = frozenset({"record", "streak"})


def _token(seed: int, real_id: str) -> str:
    """A short opaque token for ``real_id``, deterministic in ``(seed, real_id)``
    and independent of every other id in the bundle."""
    rng = random.Random(f"{seed}\x1f{real_id}")
    return "id_" + "".join(rng.choice(_TOKEN_ALPHABET) for _ in range(10))


def _looks_like_real_id(value: Any) -> bool:
    """A numeric string (>= 6 digits) or a large int — plausibly a Sleeper id,
    never a roster id (1-32), week, slot, or a current NFL ``player_id``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value.isdigit() and len(value) >= 6
    if isinstance(value, int):
        return value >= 100_000
    return False


def _require_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object, got {type(value).__name__}")
    return value


class _Anonymizer:
    def __init__(self, bundle: dict[str, Any], seed: int) -> None:
        self.src = bundle
        self.seed = seed
        self._id_cache: dict[str, str] = {}

        # One {display_name, team_name} persona per real account id, assigned from
        # a seed-shuffled pool in sorted-id order so every slice of one league
        # agrees. Owners / co-owners / draft creators that never appear in
        # ``users`` still get their own persona (not a shared fallback).
        pool = list(NAME_POOL)
        random.Random(seed).shuffle(pool)

        persona_ids = sorted(self._collect_persona_ids())
        needed = 2 * len(persona_ids) + 1
        if needed > len(pool):
            raise ValueError(
                f"NAME_POOL too small: {len(persona_ids)} members need {needed} "
                f"names, pool has {len(pool)}"
            )
        self._persona: dict[str, dict[str, str]] = {}
        cursor = iter(pool)
        for pid in persona_ids:
            self._persona[pid] = {
                "display_name": next(cursor),
                "team_name": next(cursor),
            }
        self._league_name = next(cursor)

    def _collect_persona_ids(self) -> set[str]:
        ids: set[str] = set()
        for user in self.src["users"]:
            if isinstance(user, dict) and user.get("user_id") is not None:
                ids.add(str(user["user_id"]))
        for roster in self.src["rosters"]:
            if not isinstance(roster, dict):
                continue
            if roster.get("owner_id") is not None:
                ids.add(str(roster["owner_id"]))
            co = roster.get("co_owners")
            if isinstance(co, str):
                ids.add(co)
            elif isinstance(co, list):
                ids.update(str(x) for x in co if x is not None)
        draft = self.src.get("draft") or {}
        if isinstance(draft, dict):
            for c in draft.get("creators") or []:
                if c is not None:
                    ids.add(str(c))
            order = draft.get("draft_order")
            if isinstance(order, dict):
                ids.update(str(k) for k in order)
        return ids

    # -- helpers --------------------------------------------------------------- #

    def tok(self, value: Any) -> Any:
        if value is None:
            return None
        key = str(value)
        cached = self._id_cache.get(key)
        if cached is None:
            cached = _token(self.seed, key)
            self._id_cache[key] = cached
        return cached

    def persona(self, real_id: Any) -> dict[str, str]:
        key = str(real_id)
        found = self._persona.get(key)
        if found is not None:
            return found
        rng = random.Random(f"{self.seed}\x1fpersona\x1f{key}")
        return {
            "display_name": rng.choice(NAME_POOL),
            "team_name": rng.choice(NAME_POOL),
        }

    def _tok_list(self, value: Any) -> Any:
        if value is None:
            return None
        items = value if isinstance(value, list) else [value]
        return [self.tok(x) for x in items]

    # -- section builders ----------------------------------------------------- #

    def league(self) -> dict[str, Any]:
        src = _require_dict(self.src["league"], "league")
        if src.get("scoring_settings") is None:
            raise ValueError("league.scoring_settings is missing — cannot preserve it")
        if src.get("roster_positions") is None:
            raise ValueError("league.roster_positions is missing — cannot preserve it")
        md = src.get("metadata") or {}
        clean_md = {k: md[k] for k in _LEAGUE_META_KEYS if k in md}
        return {
            "name": self._league_name,
            "season": src.get("season"),
            "season_type": src.get("season_type"),
            "sport": src.get("sport"),
            "status": src.get("status"),
            "total_rosters": src.get("total_rosters"),
            "roster_positions": src.get("roster_positions"),
            "scoring_settings": src.get("scoring_settings"),
            "settings": src.get("settings"),
            "metadata": clean_md,
            "league_id": self.tok(src.get("league_id")),
            "draft_id": self.tok(src.get("draft_id")),
            "previous_league_id": self.tok(src.get("previous_league_id")),
        }

    def users(self) -> list[dict[str, Any]]:
        out = []
        for i, u in enumerate(self.src["users"]):
            u = _require_dict(u, f"users[{i}]")
            p = self.persona(u.get("user_id"))
            out.append(
                {
                    "user_id": self.tok(u.get("user_id")),
                    "league_id": self.tok(u.get("league_id")),
                    "display_name": p["display_name"],
                    "is_bot": u.get("is_bot", False),
                    "is_owner": u.get("is_owner"),
                    "metadata": {"team_name": p["team_name"]},
                }
            )
        return out

    def rosters(self) -> list[dict[str, Any]]:
        out = []
        for i, r in enumerate(self.src["rosters"]):
            r = _require_dict(r, f"rosters[{i}]")
            md = r.get("metadata") or {}
            out.append(
                {
                    "roster_id": r.get("roster_id"),
                    "owner_id": self.tok(r.get("owner_id")),
                    "co_owners": self._tok_list(r.get("co_owners")),
                    "league_id": self.tok(r.get("league_id")),
                    "keepers": r.get("keepers"),
                    "player_map": r.get("player_map"),
                    "players": r.get("players"),
                    "starters": r.get("starters"),
                    "reserve": r.get("reserve"),
                    "taxi": r.get("taxi"),
                    "settings": r.get("settings"),
                    "metadata": {k: md[k] for k in _ROSTER_META_KEYS if k in md},
                }
            )
        return out

    def _week_map(self, section: str, builder) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for week, rows in (self.src.get(section) or {}).items():
            built = []
            for j, row in enumerate(rows or []):
                built.append(builder(_require_dict(row, f"{section}[{week}][{j}]")))
            out[str(week)] = built
        return out

    def _matchup(self, m: dict[str, Any]) -> dict[str, Any]:
        return {k: m.get(k) for k in _MATCHUP_FIELDS if k in m}

    def _transaction(self, t: dict[str, Any]) -> dict[str, Any]:
        picks = t.get("draft_picks") or []
        return {
            "type": t.get("type"),
            "status": t.get("status"),
            "created": t.get("created"),
            "status_updated": t.get("status_updated"),
            "leg": t.get("leg"),
            "settings": t.get("settings"),
            "metadata": {},  # Sleeper's trade/waiver notes are commissioner free text
            "transaction_id": self.tok(t.get("transaction_id")),
            "creator": self.tok(t.get("creator")),
            "adds": t.get("adds"),
            "drops": t.get("drops"),
            "consenter_ids": t.get("consenter_ids"),
            "roster_ids": t.get("roster_ids"),
            "waiver_budget": t.get("waiver_budget"),
            "draft_picks": [
                {
                    "season": p.get("season"),
                    "round": p.get("round"),
                    "roster_id": p.get("roster_id"),
                    "owner_id": p.get("owner_id"),
                    "previous_owner_id": p.get("previous_owner_id"),
                    "league_id": self.tok(p.get("league_id")),
                }
                for p in picks
                if isinstance(p, dict)
            ],
        }

    def draft(self) -> dict[str, Any] | None:
        src = self.src.get("draft")
        if src is None:
            return None
        src = _require_dict(src, "draft")
        order = src.get("draft_order") or {}
        md = src.get("metadata") or {}
        return {
            "type": src.get("type"),
            "status": src.get("status"),
            "sport": src.get("sport"),
            "season": src.get("season"),
            "season_type": src.get("season_type"),
            "settings": src.get("settings"),
            "slot_to_roster_id": src.get("slot_to_roster_id"),
            "draft_order": {
                self.tok(uid): slot
                for uid, slot in (order.items() if isinstance(order, dict) else [])
            },
            "metadata": {
                "name": self._league_name,
                "scoring_type": md.get("scoring_type"),
            },
            "draft_id": self.tok(src.get("draft_id")),
            "league_id": self.tok(src.get("league_id")),
            "creators": [self.tok(c) for c in (src.get("creators") or [])],
        }

    def draft_picks(self) -> list[dict[str, Any]]:
        out = []
        for pick in self.src.get("draft_picks") or []:
            if not isinstance(pick, dict):
                continue
            md = pick.get("metadata") or {}
            out.append(
                {
                    "draft_id": self.tok(pick.get("draft_id")),
                    "picked_by": self.tok(pick.get("picked_by")),
                    "roster_id": pick.get("roster_id"),
                    "round": pick.get("round"),
                    "draft_slot": pick.get("draft_slot"),
                    "pick_no": pick.get("pick_no"),
                    "is_keeper": pick.get("is_keeper"),
                    "player_id": pick.get("player_id"),
                    "metadata": {
                        k: md[k] for k in _DRAFT_PICK_META_FIELDS if k in md
                    },
                }
            )
        return out

    def players(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for pid, rec in (self.src.get("players") or {}).items():
            rec = rec if isinstance(rec, dict) else {}
            trimmed = {f: rec[f] for f in _PLAYER_FIELDS if f in rec}
            rec_md = rec.get("metadata")
            if isinstance(rec_md, dict):
                # rec_md fields fill in only where the top level did not supply them.
                for f in _PLAYER_META_FIELDS:
                    if f not in trimmed and f in rec_md:
                        trimmed[f] = rec_md[f]
            out[str(pid)] = trimmed
        return out

    # -- driver --------------------------------------------------------------- #

    def run(self) -> dict[str, Any]:
        meta_src = _require_dict(self.src["meta"], "meta")
        result = {
            "meta": {
                k: meta_src[k]
                for k in ("case", "target_week", "exercises")
                if k in meta_src
            },
            "league": self.league(),
            "users": self.users(),
            "rosters": self.rosters(),
            "matchups": self._week_map("matchups", self._matchup),
            "transactions": self._week_map("transactions", self._transaction),
            "draft": self.draft(),
            "draft_picks": self.draft_picks(),
            "traded_picks": self.src.get("traded_picks") or [],
            "winners_bracket": self.src.get("winners_bracket") or [],
            "losers_bracket": self.src.get("losers_bracket") or [],
            "players": self.players(),
        }
        # Safety net over the whole result: tokenize any id-typed key's value and
        # any 15+-digit run that a passthrough subtree (settings, adds/drops, a
        # contributor's schema drift) still carries. Roster ids, weeks, slots,
        # and epoch-ms timestamps (13 digits) are left alone.
        result = self._sweep(result)
        # Then flatten every real ``created`` / ``status_updated`` wall-clock onto
        # the synthetic grid, order-preserving.
        return self._remap_timestamps(result)

    def _remap_timestamps(self, result: dict[str, Any]) -> dict[str, Any]:
        distinct: set[int] = set()

        def collect(obj: Any, parent_key: str | None) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    collect(v, k)
            elif isinstance(obj, list):
                for v in obj:
                    collect(v, parent_key)
            elif parent_key in _TIMESTAMP_KEYS:
                epoch = _coerce_epoch(obj)
                if epoch is not None:
                    distinct.add(epoch)

        collect(result, None)
        if not distinct:
            return result

        grid = {
            real: SYNTH_EPOCH_BASE_MS + rank * SYNTH_EPOCH_STEP_MS
            for rank, real in enumerate(sorted(distinct))
        }

        def rewrite(obj: Any, parent_key: str | None) -> Any:
            if isinstance(obj, dict):
                return {k: rewrite(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [rewrite(v, parent_key) for v in obj]
            if parent_key in _TIMESTAMP_KEYS:
                epoch = _coerce_epoch(obj)
                if epoch is not None and epoch in grid:
                    return grid[epoch]
            return obj

        return rewrite(result, None)

    def _sweep(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            swept: dict[Any, Any] = {}
            for k, v in obj.items():
                key = self.tok(k) if isinstance(k, str) and _DIGIT_RUN_15.fullmatch(k) else k
                if k in _SCALAR_ID_KEYS and _looks_like_real_id(v):
                    swept[key] = self.tok(v)
                elif k in _LIST_ID_KEYS and isinstance(v, list):
                    swept[key] = [
                        self.tok(x) if _looks_like_real_id(x) else self._sweep(x)
                        for x in v
                    ]
                else:
                    swept[key] = self._sweep(v)
            return swept
        if isinstance(obj, list):
            return [self._sweep(v) for v in obj]
        if isinstance(obj, str):
            return _DIGIT_RUN_15.sub(lambda m: self.tok(m.group()), obj)
        if isinstance(obj, int) and not isinstance(obj, bool) and abs(obj) >= 10**14:
            return self.tok(obj)
        return obj


def anonymize_bundle(bundle: dict[str, Any], seed: int = 0) -> dict[str, Any]:
    """Return an anonymized copy of ``bundle`` in the same shape.

    ``bundle`` must already be structurally valid (see :func:`load_bundle`); this
    function re-validates defensively so it is safe to call on its own. Raises
    ``ValueError`` on a bundle that validates structurally but cannot be
    anonymized (missing ``scoring_settings`` / ``roster_positions``, a member
    pool too small for the league, a non-object list item).
    """
    validated = load_bundle(bundle)
    return _Anonymizer(validated, seed).run()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anonymize.py",
        description=(
            "Anonymize one raw Sleeper league bundle into a committable fixture. "
            "Reads a path (or '-' / nothing for stdin), writes JSON to stdout."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="-",
        help="raw bundle JSON file; '-' or omitted reads stdin",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="deterministic seed for name/id assignment (default: 0)",
    )
    return parser


def _terse_validation_error(exc: ValidationError) -> str:
    problems = exc.errors()
    parts = []
    for err in problems[:10]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return f"({len(problems)} problem(s)) " + "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.path == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(args.path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"anonymize: cannot read {args.path}: {exc}", file=sys.stderr)
            return 2

    try:
        bundle = load_bundle(raw)
    except json.JSONDecodeError as exc:
        print(f"anonymize: input is not valid JSON: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        # Never echo the full pydantic error — it embeds ``input_value`` fragments
        # of the raw, un-anonymized bundle into CI logs.
        print(
            f"anonymize: bundle failed validation {_terse_validation_error(exc)}",
            file=sys.stderr,
        )
        return 1

    try:
        result = anonymize_bundle(bundle, args.seed)
    except (ValueError, StopIteration) as exc:
        print(f"anonymize: cannot anonymize bundle: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
