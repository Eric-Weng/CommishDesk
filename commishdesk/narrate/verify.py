"""Content-safety P1 — claim extraction + deterministic adjudication.

The closed-world check (``narrate/safety.py``) refutes *tokens*: a number or a
letter grade the payload does not contain. It cannot see **recombination** —
every token real, the pairing wrong. Live validation measured exactly that as
the last class still shipping: "Miami tight end Jalin Conyers", when the payload
says Texas Tech, in 4 of 40 Issues (4 of 1,342 attributive claims).

This module closes it in two halves, split on purpose:

* **Extraction** — one cheap model call turns the finished prose into typed
  claim tuples: who was picked where, whose college, which grade, how many at a
  position. The extractor never sees the Facts, so it cannot quietly "correct" a
  wrong claim into a right one, and it renders no verdicts at all.
* **Adjudication** — pure Python looks each tuple up in the ``narration``
  projection and answers ``refuted`` / ``supported`` / ``unverifiable``. No
  model, no credentials, no network: every verdict is reproducible, and every
  refutation names the payload value it contradicts.

The operative rule is the one the whole content-safety rework runs on: a claim
is **refuted** only when the payload positively contradicts it *and* its
sentence can be found in the prose. Anything else — an unknown or ambiguous
player, a field the payload leaves empty, a sentence the extractor paraphrased
past recognition — is ``unverifiable``. That is logged as a coverage measure and
never blocks anything.

It fails **open**. An unconfigured verifier, a provider fault, a truncated or
unparseable extraction: each yields a result with ``ran=False``, and the caller
ships on the deterministic checks alone. Verification must never become a new
way for an Issue not to go out.

Import fence: stdlib + pydantic + ``commishdesk.narrate.safety``. The one paid
call goes through ``narrate/llm.py``'s single ``generate`` call site (I3's
structural guard), and that module is imported lazily inside
:func:`verify_narration`, so :func:`parse_claims` and :func:`adjudicate` stay
importable and testable with no provider anywhere in sight.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from commishdesk.narrate.safety import (
    CATEGORY_SEVERITY,
    SafetyFinding,
    SafetyReport,
    _normalize,
    _sentences,
)

if TYPE_CHECKING:
    from commishdesk.facts.schema import Narration, NarrationPlayer, NarrationTeam
    from commishdesk.llmconfig import LLMModelConfig
    from commishdesk.narrate.llm import LLMClient
    from commishdesk.voices import Voice

__all__ = [
    "EXTRACTOR_VOICE",
    "MAX_CLAIMS",
    "ClaimKind",
    "ClaimStatus",
    "ClaimVerdict",
    "ExtractedClaim",
    "VerificationResult",
    "adjudicate",
    "parse_claims",
    "verify_narration",
]

ClaimKind = Literal["pick", "college", "grade", "pick_count", "position_count", "consensus"]
ClaimStatus = Literal["supported", "refuted", "unverifiable"]

#: Upper bound on claims adjudicated per Issue. A real recap carries a few dozen;
#: this only stops a runaway extraction from costing unbounded CPU.
MAX_CLAIMS = 400

_EXTRACTOR_PROMPT = """\
You extract factual claims from a fantasy football draft recap. You do not judge
whether a claim is true, you never correct one, and you never add anything the
text does not say. Output JSON only.

Extract every place the text asserts one of these, and nothing else:

- "pick": a specific player went at a specific spot, or to a specific fantasy
  team. Fields: player, plus whichever the sentence states of pick_no (the
  overall pick number, e.g. 14), board_label (round.slot exactly as written,
  e.g. "2.02"), manager (the fantasy team name as written), position.
- "college": a player's college or school. Fields: player, college.
- "grade": a fantasy team's draft grade. Fields: manager, grade (e.g. "B+").
- "pick_count": how many picks in total a fantasy team made. Fields: manager,
  count.
- "position_count": how many players at one position a fantasy team drafted.
  Fields: manager, position, count.
- "consensus": a player's consensus slot, or how many slots above or below
  consensus they went. Fields: player, plus consensus_label (e.g. "1.04") and/or
  delta (the number of slots).

Rules:
1. "sentence" is the full sentence the claim comes from, copied exactly,
   character for character.
2. One claim per assertion. A sentence stating three facts yields three claims.
3. Copy every name and value exactly as the text writes it, even if you believe
   it is wrong. Never substitute anything you know about real players.
4. Include only the fields the sentence actually states. Omit the rest.
5. Write numbers as digits ("three" becomes 3).
6. Ignore opinions, jokes, predictions, and anything not in the list above.

Output exactly this shape:
{"claims": [{"kind": "pick", "sentence": "...", "player": "...", "board_label": "..."}]}
If there are no such claims, output {"claims": []}
"""


@dataclass(frozen=True, slots=True)
class _ExtractorVoice:
    """The extractor's "voice": a system prompt and nothing else. It satisfies the
    ``Voice`` protocol only because ``LLMClient.generate`` takes one."""

    system_prompt: str
    banned_topics: frozenset[str]
    voice_id: str


#: ``# type: ignore[assignment]``: a frozen dataclass exposes read-only attributes,
#: which mypy will not match against ``Voice``'s settable protocol members — the
#: same false positive ``voices/beat_writer.py`` documents.
EXTRACTOR_VOICE: Voice = _ExtractorVoice(  # type: ignore[assignment]
    system_prompt=_EXTRACTOR_PROMPT,
    banned_topics=frozenset(),
    voice_id="claim-extractor",
)


# --------------------------------------------------------------------------- #
# Extraction output
# --------------------------------------------------------------------------- #


class ExtractedClaim(BaseModel):
    """One claim as the extractor wrote it. Nothing here has been checked."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: ClaimKind
    sentence: str
    player: str | None = None
    manager: str | None = None
    pick_no: int | None = None
    board_label: str | None = None
    college: str | None = None
    grade: str | None = None
    count: int | None = None
    position: str | None = None
    consensus_label: str | None = None
    delta: int | None = None

    @field_validator("pick_no", "count", "delta", mode="before")
    @classmethod
    def _int_or_none(cls, value: object) -> object:
        """``14`` / ``"14"`` / ``"+10"`` -> an int; anything unparseable -> None
        (an unstated field, never a guess)."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            return int(text) if re.fullmatch(r"[+-]?\d+", text) else None
        return None

    @field_validator(
        "player", "manager", "board_label", "college", "grade", "position", "consensus_label", mode="before"
    )
    @classmethod
    def _text_or_none(cls, value: object) -> object:
        """Only a non-blank string survives. A board label that arrives as a JSON
        number (``2.1`` for "2.10") has already lost the digit it would be
        compared on, so it is dropped rather than guessed at."""
        if isinstance(value, str):
            return value.strip() or None
        return None


def parse_claims(raw: str) -> tuple[tuple[ExtractedClaim, ...], int] | None:
    """Parse the extractor's completion into ``(claims, unparsed_count)``.

    ``None`` means the completion as a whole is unusable (not JSON, or no claim
    list in it) — the caller fails open. A single malformed claim does not sink
    the rest: it is skipped and counted in ``unparsed_count``.
    """
    text = re.sub(r"^\s*```[A-Za-z]*\s*|\s*```\s*$", "", raw.strip())
    data: object = None
    for candidate in (text, _outermost(text, "{", "}"), _outermost(text, "[", "]")):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        break
    if isinstance(data, dict):
        items = data.get("claims")
    elif isinstance(data, list):
        items = data
    else:
        return None
    if not isinstance(items, list):
        return None

    claims: list[ExtractedClaim] = []
    unparsed = 0
    for item in items:
        if not isinstance(item, dict):
            unparsed += 1
            continue
        try:
            claim = ExtractedClaim.model_validate(item)
        except ValidationError:
            unparsed += 1
            continue
        if not claim.sentence.strip():
            unparsed += 1
            continue
        claims.append(claim)
    return tuple(claims), unparsed


def _outermost(text: str, open_char: str, close_char: str) -> str | None:
    start, end = text.find(open_char), text.rfind(close_char)
    return text[start : end + 1] if start != -1 and end > start else None


# --------------------------------------------------------------------------- #
# Normalisation — both sides of every comparison go through the same function
# --------------------------------------------------------------------------- #

_PUNCT_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-"})
_MARKUP = re.compile(r"[*_`#]")


def _fold(text: str) -> str:
    """Normalized, markdown-stripped, whitespace-collapsed, casefolded."""
    folded = _MARKUP.sub("", _normalize(text).translate(_PUNCT_FOLD))
    return re.sub(r"\s+", " ", folded).strip().casefold()


def _name_key(name: str) -> str:
    """A person or team name reduced to comparable tokens: apostrophes and periods
    dropped, hyphens as spaces, generational suffixes removed."""
    key = re.sub(r"[.']", "", _fold(name)).replace("-", " ")
    key = re.sub(r"[^\w ]", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", key)


def _team_key(name: str) -> str:
    return re.sub(r"^the ", "", _name_key(name))


def _college_key(name: str) -> str:
    """ "Miami (FL)" and "Miami" compare equal; "Ohio St." and "Ohio State" compare
    equal; "Arizona" and "Arizona State" do not — they are different schools."""
    key = re.sub(r"\([^)]*\)", " ", _fold(name))
    key = re.sub(r"^the ", "", key.strip())
    key = re.sub(r"^university of ", "", key)
    key = re.sub(r" university$", "", key)
    key = re.sub(r"\bst\.?$", "state", key)
    key = key.replace("&", " and ")
    return re.sub(r"\s+", " ", key).strip()


_LABEL = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\s*$")


def _label_key(label: str | None) -> tuple[int, int] | None:
    match = _LABEL.match(label or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


_POSITION_WORDS = {
    "quarterback": "QB",
    "quarterbacks": "QB",
    "running back": "RB",
    "running backs": "RB",
    "tailback": "RB",
    "tailbacks": "RB",
    "wide receiver": "WR",
    "wide receivers": "WR",
    "receiver": "WR",
    "receivers": "WR",
    "wideout": "WR",
    "wideouts": "WR",
    "tight end": "TE",
    "tight ends": "TE",
}
_POSITION_CODES = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})


def _position_key(text: str | None) -> str | None:
    if not text:
        return None
    words = re.sub(r"[^a-z ]", "", text.casefold()).strip()
    if words in _POSITION_WORDS:
        return _POSITION_WORDS[words]
    code = re.sub(r"s$", "", words).upper()
    return code if code in _POSITION_CODES else None


def _grade_letter(grade: str | None) -> str | None:
    match = re.fullmatch(r"\s*([A-Fa-f])\s*[+-]?\s*", grade or "")
    return match.group(1).upper() if match else None


# --------------------------------------------------------------------------- #
# Payload lookups — ambiguity is always "not found", never a best guess
# --------------------------------------------------------------------------- #


def _find_player(name: str | None, narration: Narration) -> NarrationPlayer | None:
    if not name:
        return None
    key = _name_key(name)
    if not key:
        return None
    exact = [p for p in narration.players if _name_key(p.name) == key]
    if exact:
        return exact[0] if len(exact) == 1 else None
    tokens = key.split()
    last, first = tokens[-1], (tokens[0] if len(tokens) > 1 else None)
    matches = []
    for player in narration.players:
        ptokens = _name_key(player.name).split()
        if not ptokens or ptokens[-1] != last:
            continue
        if first is None or ptokens[0] == first or ptokens[0].startswith(first):
            matches.append(player)
    return matches[0] if len(matches) == 1 else None


def _find_team(name: str | None, narration: Narration) -> NarrationTeam | None:
    if not name:
        return None
    key = _team_key(name)
    if not key:
        return None
    teams = [t for t in narration.teams if t.manager]
    exact = [t for t in teams if _team_key(t.manager or "") == key]
    if exact:
        return exact[0] if len(exact) == 1 else None
    needle = key.split()
    partial = [t for t in teams if _contains_run(_team_key(t.manager or "").split(), needle)]
    return partial[0] if len(partial) == 1 else None


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    size = len(needle)
    return any(haystack[i : i + size] == needle for i in range(len(haystack) - size + 1))


# --------------------------------------------------------------------------- #
# Adjudication
# --------------------------------------------------------------------------- #

_Check = tuple[ClaimStatus, str, str]


def _compare(field: str, who: str, claimed: object, actual: object, *, same: bool) -> _Check:
    if same:
        return ("supported", f"{who}: {field} {claimed!r} matches the payload", str(claimed))
    return ("refuted", f"{who}: {field} claimed {claimed!r}, payload has {actual!r}", str(claimed))


def _unverifiable(reason: str) -> list[_Check]:
    return [("unverifiable", reason, "")]


def _judge_pick(claim: ExtractedClaim, narration: Narration) -> list[_Check]:
    player = _find_player(claim.player, narration)
    if player is None:
        return _unverifiable(f"player {claim.player!r} is not uniquely in the payload")
    checks: list[_Check] = []
    if claim.pick_no is not None and player.pick_no is not None:
        checks.append(
            _compare("pick", player.name, claim.pick_no, player.pick_no, same=claim.pick_no == player.pick_no)
        )
    claimed_label, actual_label = _label_key(claim.board_label), _label_key(player.board_label)
    if claimed_label is not None and actual_label is not None:
        checks.append(
            _compare(
                "board slot",
                player.name,
                claim.board_label,
                player.board_label,
                same=claimed_label == actual_label,
            )
        )
    if claim.manager and player.manager:
        team = _find_team(claim.manager, narration)
        if team is not None and team.manager:
            checks.append(
                _compare(
                    "drafting team",
                    player.name,
                    claim.manager,
                    player.manager,
                    same=_team_key(team.manager) == _team_key(player.manager),
                )
            )
    claimed_pos, actual_pos = _position_key(claim.position), _position_key(player.position)
    if claimed_pos is not None and actual_pos is not None:
        checks.append(
            _compare("position", player.name, claim.position, player.position, same=claimed_pos == actual_pos)
        )
    return checks or _unverifiable(f"{player.name}: no pick detail the payload can check")


def _judge_college(claim: ExtractedClaim, narration: Narration) -> list[_Check]:
    player = _find_player(claim.player, narration)
    if player is None:
        return _unverifiable(f"player {claim.player!r} is not uniquely in the payload")
    if not claim.college or not player.college:
        # Absent on the live path by design (no /players/nfl fetch): not knowing a
        # college is never grounds to refute one.
        return _unverifiable(f"{player.name}: the payload carries no college to check against")
    return [
        _compare(
            "college",
            player.name,
            claim.college,
            player.college,
            same=_college_key(claim.college) == _college_key(player.college),
        )
    ]


def _team_or_unverifiable(claim: ExtractedClaim, narration: Narration) -> NarrationTeam | None:
    return _find_team(claim.manager, narration)


def _judge_grade(claim: ExtractedClaim, narration: Narration) -> list[_Check]:
    team = _team_or_unverifiable(claim, narration)
    claimed = _grade_letter(claim.grade)
    if team is None or claimed is None:
        return _unverifiable(f"grade claim for {claim.manager!r} is not checkable")
    actual = _grade_letter(team.grade)
    if actual is None:
        return _unverifiable(f"{team.manager}: payload grade {team.grade!r} is not a letter grade")
    # The letter decides it. "an A" for an A+ is loose phrasing, not an invented
    # grade, and a sentence is not worth excising over the modifier.
    return [_compare("grade", team.manager or "", claim.grade, team.grade, same=claimed == actual)]


def _judge_pick_count(claim: ExtractedClaim, narration: Narration) -> list[_Check]:
    team = _team_or_unverifiable(claim, narration)
    if team is None or claim.count is None:
        return _unverifiable(f"pick-count claim for {claim.manager!r} is not checkable")
    return [
        _compare("pick count", team.manager or "", claim.count, team.pick_count, same=claim.count == team.pick_count)
    ]


def _judge_position_count(claim: ExtractedClaim, narration: Narration) -> list[_Check]:
    team = _team_or_unverifiable(claim, narration)
    position = _position_key(claim.position)
    if team is None or position is None or claim.count is None:
        return _unverifiable(f"position-count claim for {claim.manager!r} is not checkable")
    if position not in team.positional_counts:
        return _unverifiable(f"{team.manager}: payload has no {position} count")
    actual = team.positional_counts[position]
    return [_compare(f"{position} count", team.manager or "", claim.count, actual, same=claim.count == actual)]


def _consensus_entries(narration: Narration) -> list[tuple[str, str | None, int | None]]:
    """Every ``(player, consensus_label, delta)`` the projection states anywhere."""
    entries: list[tuple[str, str | None, int | None]] = [
        (pick.player, pick.consensus_label, pick.delta) for pick in narration.board_round1
    ]
    sup = narration.superlatives
    for pick in (sup.best_value, sup.best_value_runner_up, sup.biggest_reach, sup.biggest_reach_runner_up):
        if pick is not None:
            entries.append((pick.player, pick.consensus_label, pick.delta))
    if sup.boldest_swing is not None:
        entries.extend((pick.player, pick.consensus_label, pick.delta) for pick in sup.boldest_swing.picks)
    for team in narration.teams:
        for extreme in (team.best_value_pick, team.biggest_reach_pick):
            if extreme is not None:
                entries.append((extreme.player, None, extreme.delta))
    return entries


def _judge_consensus(claim: ExtractedClaim, narration: Narration) -> list[_Check]:
    player = _find_player(claim.player, narration)
    if player is None:
        return _unverifiable(f"player {claim.player!r} is not uniquely in the payload")
    key = _name_key(player.name)
    mine = [entry for entry in _consensus_entries(narration) if _name_key(entry[0]) == key]
    labels = {label for _, raw, _ in mine if (label := _label_key(raw)) is not None}
    deltas = {abs(delta) for _, _, delta in mine if delta is not None}
    checks: list[_Check] = []
    claimed_label = _label_key(claim.consensus_label)
    if claimed_label is not None and labels:
        known = ", ".join(sorted(f"{r}.{s:02d}" for r, s in labels))
        checks.append(
            _compare("consensus slot", player.name, claim.consensus_label, known, same=claimed_label in labels)
        )
    if claim.delta is not None and deltas:
        # Magnitude only. The payload's sign convention (positive = value) is not
        # one prose follows — "reached three slots" is a delta of -3 — so a sign
        # disagreement is a phrasing difference, not a refutable fact.
        known = ", ".join(str(d) for d in sorted(deltas))
        checks.append(_compare("slots vs consensus", player.name, claim.delta, known, same=abs(claim.delta) in deltas))
    return checks or _unverifiable(f"{player.name}: the payload states no consensus detail to check")


_JUDGES: dict[str, Callable[[ExtractedClaim, Narration], list[_Check]]] = {
    "pick": _judge_pick,
    "college": _judge_college,
    "grade": _judge_grade,
    "pick_count": _judge_pick_count,
    "position_count": _judge_position_count,
    "consensus": _judge_consensus,
}


def _combine(checks: list[_Check]) -> _Check:
    refuted = [check for check in checks if check[0] == "refuted"]
    if refuted:
        return ("refuted", "; ".join(check[1] for check in refuted), refuted[0][2])
    supported = [check for check in checks if check[0] == "supported"]
    if supported:
        return ("supported", "; ".join(check[1] for check in supported), supported[0][2])
    return ("unverifiable", checks[0][1] if checks else "nothing checkable was asserted", "")


def _anchor(claim: ExtractedClaim) -> str | None:
    """The token a claim's own sentence must contain: the player's last name, or
    the fantasy team's last word."""
    name = claim.player if claim.kind in ("pick", "college", "consensus") else claim.manager
    tokens = _fold(name or "").split()
    return tokens[-1] if tokens else None


def _locate(claim: ExtractedClaim, sentences: tuple[str, ...], folded: tuple[str, ...]) -> str | None:
    """The prose sentence *claim* came from, or ``None``. A refutation has to be
    able to point at the prose as well as at the payload — a claim whose sentence
    cannot be found was paraphrased or invented by the extractor, not written by
    the narrator."""
    target = _fold(claim.sentence)
    if len(target) < 8:
        return None
    candidates = [i for i, text in enumerate(folded) if len(text) >= 8 and (text in target or target in text)]
    anchor = _anchor(claim)
    if anchor is not None:
        candidates = [i for i in candidates if anchor in folded[i]]
    if not candidates:
        return None
    values = [
        _fold(str(v))
        for v in (claim.college, claim.grade, claim.board_label, claim.consensus_label, claim.pick_no, claim.count)
        if v is not None
    ]
    for i in candidates:
        if any(value and value in folded[i] for value in values):
            return sentences[i]
    return sentences[candidates[0]]


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """One claim, judged. ``sentence`` is the located prose sentence (a substring
    of the normalized prose, so the repair tier can excise it), or ``None``."""

    claim: ExtractedClaim
    status: ClaimStatus
    reason: str
    sentence: str | None
    matched: str


def adjudicate(claims: Iterable[ExtractedClaim], prose: str, narration: Narration) -> tuple[ClaimVerdict, ...]:
    """Judge every claim against *narration*. Pure and deterministic: the same
    inputs always produce the same verdicts, in claim order."""
    sentences = _sentences(_normalize(prose))
    folded = tuple(_fold(text) for text in sentences)
    verdicts: list[ClaimVerdict] = []
    for index, claim in enumerate(claims):
        if index >= MAX_CLAIMS:
            break
        located = _locate(claim, sentences, folded)
        if located is None:
            verdicts.append(
                ClaimVerdict(claim, "unverifiable", "the claim's sentence was not found in the prose", None, "")
            )
            continue
        status, reason, matched = _combine(_JUDGES[claim.kind](claim, narration))
        verdicts.append(ClaimVerdict(claim, status, reason, located, matched))
    return tuple(verdicts)


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What one verification pass found. ``ran=False`` means it did not happen
    (disabled, or failed open — ``error`` says why) and nothing may be inferred
    from the empty verdicts."""

    ran: bool
    verdicts: tuple[ClaimVerdict, ...] = ()
    error: str | None = None
    unparsed: int = 0

    @property
    def refuted(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == "refuted")

    @property
    def supported(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == "supported")

    @property
    def unverifiable(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == "unverifiable")

    @property
    def unverifiable_share(self) -> float | None:
        """The coverage measure content-safety decision 2 budgets (≤ 8% of
        claims): how much of what the narrator asserted the adjudicator could
        not reach. ``None`` when there were no claims."""
        total = len(self.verdicts) + self.unparsed
        return (len(self.unverifiable) + self.unparsed) / total if total else None

    def report(self) -> SafetyReport:
        """The refutations as ``hallucination`` findings — the same tier the
        closed-world check uses, so the repair tier excises them unchanged."""
        findings: list[SafetyFinding] = []
        seen: set[tuple[str, str]] = set()
        for verdict in self.refuted:
            sentence = verdict.sentence or ""
            if (sentence, verdict.reason) in seen:
                continue
            seen.add((sentence, verdict.reason))
            findings.append(
                SafetyFinding(
                    category="hallucination",
                    severity=CATEGORY_SEVERITY["hallucination"],
                    message=f"refuted {verdict.claim.kind} claim: {verdict.reason}",
                    sentence=sentence,
                    matched=verdict.matched or verdict.claim.kind,
                )
            )
        return SafetyReport(findings=tuple(findings))


def verify_narration(
    prose: str,
    narration: Narration,
    config: LLMModelConfig | None,
    *,
    client_factory: Callable[[LLMModelConfig], LLMClient] | None = None,
) -> VerificationResult:
    """Extract claims from *prose* with the verifier model and adjudicate them.

    ``config=None`` (verification switched off) returns ``ran=False`` with no
    error and makes no call. Every failure — building the client, the provider
    call, an unparseable completion — returns ``ran=False`` with ``error`` set.
    Nothing raises: verification fails open.
    """
    if config is None:
        return VerificationResult(ran=False)
    from commishdesk.narrate import llm

    try:
        raw = llm.extract_with_llm(
            prose,
            EXTRACTOR_VOICE,
            config,
            client_factory=client_factory or llm.build_client,
        )
    except Exception as exc:  # fail open: never a new way for an Issue not to ship
        return VerificationResult(ran=False, error=f"{type(exc).__name__}: {exc}")
    parsed = parse_claims(raw)
    if parsed is None:
        return VerificationResult(ran=False, error="the extractor's completion was not a JSON claim list")
    claims, unparsed = parsed
    return VerificationResult(ran=True, verdicts=adjudicate(claims, prose, narration), unparsed=unparsed)
