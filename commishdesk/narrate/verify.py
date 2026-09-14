"""Content-safety P1 — claim extraction + deterministic adjudication.

The closed-world check (``narrate/safety.py``) refutes *tokens*: a number or a
letter grade the payload does not contain. It cannot see **recombination** —
every token real, the pairing wrong: a real school or NFL team on the wrong
player, a real board slot on the wrong pick. (The case that first motivated it,
"Miami tight end Jalin Conyers", was true all along: Miami is his NFL team. So a
place or school in front of a player is an *affiliation*, checked against both.)

This module closes it in two halves, split on purpose:

* **Extraction** — one cheap model call turns the finished prose into typed
  claim tuples: who was picked where, whose school or team, which grade. The
  extractor never sees the Facts, so it cannot quietly "correct" a
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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    "GATING_KINDS",
    "MAX_CLAIMS",
    "ClaimKind",
    "ClaimStatus",
    "ClaimVerdict",
    "ExtractedClaim",
    "VerificationResult",
    "adjudicate",
    "number_sentences",
    "parse_claims",
    "verify_narration",
]

ClaimKind = Literal["pick", "affiliation", "college", "grade", "pick_count", "position_count", "consensus"]
ClaimStatus = Literal["supported", "refuted", "unverifiable"]

#: The kinds whose refutations gate shipping. Counts and consensus are still
#: adjudicated and logged as coverage, but live extraction pins them on the wrong
#: player of a multi-player sentence too often to excise anything over them.
GATING_KINDS: frozenset[str] = frozenset({"pick", "affiliation", "college", "grade"})

#: Upper bound on claims adjudicated per Issue. A real recap carries a few dozen;
#: this only stops a runaway extraction from costing unbounded CPU.
MAX_CLAIMS = 400

_EXTRACTOR_PROMPT = """\
You extract factual claims from a fantasy football draft recap. You do not judge
whether a claim is true, you never correct one, and you never add anything the
text does not say. Output JSON only.

Extract only these three kinds of claim, and nothing else:

- "affiliation": a school, college, NFL team, city, or nickname the text attaches
  to a player, as in "Carolina back Trevor Etienne", "Buckeye receiver Emeka
  Egbuka" or "Ohio State's Will Howard". Fields: player, affiliation (exactly as
  written, e.g. "Carolina", "Buckeye").
- "pick": the text puts a player at a board slot or an overall pick number.
  Fields: player, plus whichever the sentence states of board_label (round.slot
  exactly as written, e.g. "2.02") and pick_no (the overall pick number, e.g.
  14). Skip it if the sentence states neither.
- "grade": a fantasy team's draft grade. Fields: manager (the fantasy team name as
  written), grade (e.g. "B+").

Rules:
1. The text arrives one sentence per line, each prefixed with its number in
   brackets, e.g. "[3] ...". Each claim names its sentence by that number. Never
   copy the sentence itself.
2. One claim per assertion. A sentence stating three facts yields three claims.
3. Copy every name and value exactly as the text writes it, even if you believe
   it is wrong. Never substitute anything you know about real players.
4. Leave a field empty when the sentence does not state it.
5. Write numbers as digits ("three" becomes 3).
6. A consensus slot, a delta, or a count of picks is not a claim here. Ignore
   those, and opinions, jokes, and predictions.

Output one claim per line, fields separated by "|", and nothing else:
a|<sentence number>|<player>|<affiliation>
p|<sentence number>|<player>|<board_label>|<pick_no>
g|<sentence number>|<manager>|<grade>

For example:
a|3|Trevor Etienne|Carolina
p|3|Trevor Etienne|3.12|36
p|7|Ashton Jeanty|1.01|
g|12|Pull-Guard Pumas|B+

If there are no such claims, output NONE
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
    #: The claim's sentence by its 1-based number in :func:`number_sentences`.
    sentence_no: int | None = Field(default=None, validation_alias=AliasChoices("s", "sentence_no"))
    #: The sentence copied as text: the pre-numbering reply shape, still accepted.
    sentence: str = ""
    player: str | None = None
    manager: str | None = None
    pick_no: int | None = None
    board_label: str | None = None
    college: str | None = None
    affiliation: str | None = None
    grade: str | None = None
    count: int | None = None
    position: str | None = None
    consensus_label: str | None = None
    delta: int | None = None

    @field_validator("sentence_no", "pick_no", "count", "delta", mode="before")
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
        "player",
        "manager",
        "board_label",
        "college",
        "affiliation",
        "grade",
        "position",
        "consensus_label",
        mode="before",
    )
    @classmethod
    def _text_or_none(cls, value: object) -> object:
        """Only a non-blank string survives. A board label that arrives as a JSON
        number (``2.1`` for "2.10") has already lost the digit it would be
        compared on, so it is dropped rather than guessed at."""
        if isinstance(value, str):
            return value.strip() or None
        return None


#: The compact reply format: kind code -> (kind, the fields after the sentence number).
#: One line per claim costs a fraction of the output tokens of a JSON object per claim,
#: and output is most of the verifier's bill.
_LINE_FORMAT: dict[str, tuple[str, tuple[str, ...]]] = {
    "a": ("affiliation", ("player", "affiliation")),
    "p": ("pick", ("player", "board_label", "pick_no")),
    "g": ("grade", ("manager", "grade")),
}


def parse_claims(raw: str) -> tuple[tuple[ExtractedClaim, ...], int] | None:
    """Parse the extractor's completion into ``(claims, unparsed_count)``.

    Reads the compact ``kind|sentence|field|...`` lines the prompt asks for, and
    still accepts the older JSON claim list. ``None`` means the completion as a
    whole is unusable — the caller fails open. A single malformed claim does not
    sink the rest: it is skipped and counted in ``unparsed_count``.
    """
    text = re.sub(r"^\s*```[A-Za-z]*\s*|\s*```\s*$", "", raw.strip())
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1 and lines[0].strip(".").casefold() == "none":
        return (), 0
    rows = [line for line in lines if "|" in line]
    if text[:1] in "{[" or not rows:
        return _parse_json_claims(text)

    claims: list[ExtractedClaim] = []
    unparsed = 0
    for row in rows:
        parts = [part.strip() for part in row.lstrip("-*• ").split("|")]
        spec = _LINE_FORMAT.get(parts[0].casefold())
        if spec is None:
            unparsed += 1
            continue
        kind, fields = spec
        values = parts[2:]
        if kind == "pick" and len(values) == len(fields) - 1:
            values.append("")
        if len(values) != len(fields):
            unparsed += 1
            continue
        if kind == "pick" and not any(values[1:]):
            continue  # a player with no slot or pick number asserts nothing checkable
        if not _append_claim({"kind": kind, "s": parts[1], **dict(zip(fields, values, strict=True))}, claims):
            unparsed += 1
    return tuple(claims), unparsed


def _append_claim(item: object, claims: list[ExtractedClaim]) -> bool:
    """Validate one claim into *claims*; ``False`` when it is malformed."""
    if not isinstance(item, dict):
        return False
    try:
        claim = ExtractedClaim.model_validate(item)
    except ValidationError:
        return False
    if claim.sentence_no is None and not claim.sentence.strip():
        return False
    claims.append(claim)
    return True


def _parse_json_claims(text: str) -> tuple[tuple[ExtractedClaim, ...], int] | None:
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
    unparsed = sum(0 if _append_claim(item, claims) else 1 for item in items)
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


def _affiliation_key(text: str) -> str:
    """A school, team or place as written, reduced for comparison: "Carolina-bound"
    and "Ohio State's" lose their suffix and a trailing plural goes, so "Buckeyes"
    and "Buckeye" compare equal. Both sides of a comparison always go through here."""
    key = re.sub(r"(?:-bound|'s)$", "", _college_key(text))
    key = re.sub(r"\s+", " ", re.sub(r"[^\w ]", " ", key)).strip()
    return key[:-1] if key.endswith("s") and not key.endswith("ss") else key


#: Sleeper's NFL team codes -> the names prose uses for each team.
_NFL_TEAM_NAMES: dict[str, tuple[str, ...]] = {
    "ARI": ("Arizona", "Cardinals"),
    "ATL": ("Atlanta", "Falcons"),
    "BAL": ("Baltimore", "Ravens"),
    "BUF": ("Buffalo", "Bills"),
    "CAR": ("Carolina", "Panthers"),
    "CHI": ("Chicago", "Bears"),
    "CIN": ("Cincinnati", "Bengals"),
    "CLE": ("Cleveland", "Browns"),
    "DAL": ("Dallas", "Cowboys"),
    "DEN": ("Denver", "Broncos"),
    "DET": ("Detroit", "Lions"),
    "GB": ("Green Bay", "Packers"),
    "HOU": ("Houston", "Texans"),
    "IND": ("Indianapolis", "Colts"),
    "JAX": ("Jacksonville", "Jaguars", "Jags"),
    "KC": ("Kansas City", "Chiefs"),
    "LV": ("Las Vegas", "Vegas", "Raiders"),
    "LAC": ("Los Angeles", "LA", "Chargers"),
    "LAR": ("Los Angeles", "LA", "Rams"),
    "MIA": ("Miami", "Dolphins"),
    "MIN": ("Minnesota", "Vikings"),
    "NE": ("New England", "Patriots", "Pats"),
    "NO": ("New Orleans", "Saints"),
    "NYG": ("New York", "Giants"),
    "NYJ": ("New York", "Jets"),
    "PHI": ("Philadelphia", "Eagles"),
    "PIT": ("Pittsburgh", "Steelers"),
    "SF": ("San Francisco", "49ers", "Niners"),
    "SEA": ("Seattle", "Seahawks"),
    "TB": ("Tampa Bay", "Tampa", "Buccaneers", "Bucs"),
    "TEN": ("Tennessee", "Titans"),
    "WAS": ("Washington", "Commanders"),
}

#: Unambiguous college nicknames -> the school. Shared ones (Tigers, Bulldogs,
#: Wildcats, Broncos) are left out: an unknown nickname is unverifiable, never refuted.
_COLLEGE_NICKNAMES: dict[str, str] = {
    "Buckeyes": "Ohio State",
    "Wolverines": "Michigan",
    "Longhorns": "Texas",
    "Crimson Tide": "Alabama",
    "Ducks": "Oregon",
    "Gators": "Florida",
    "Hurricanes": "Miami",
    "Seminoles": "Florida State",
    "Nittany Lions": "Penn State",
    "Buffaloes": "Colorado",
    "Trojans": "USC",
    "Bruins": "UCLA",
    "Tar Heels": "North Carolina",
    "Sooners": "Oklahoma",
    "Red Raiders": "Texas Tech",
    "Horned Frogs": "TCU",
    "Cyclones": "Iowa State",
    "Hawkeyes": "Iowa",
    "Badgers": "Wisconsin",
    "Volunteers": "Tennessee",
    "Vols": "Tennessee",
    "Razorbacks": "Arkansas",
    "Utes": "Utah",
    "Sun Devils": "Arizona State",
    "Fighting Irish": "Notre Dame",
    "Hokies": "Virginia Tech",
    "Bearcats": "Cincinnati",
    "Cornhuskers": "Nebraska",
    "Huskers": "Nebraska",
    "Gamecocks": "South Carolina",
    "Commodores": "Vanderbilt",
    "Blue Devils": "Duke",
    "Demon Deacons": "Wake Forest",
    "Yellow Jackets": "Georgia Tech",
    "Golden Bears": "California",
    "Beavers": "Oregon State",
    "Mountaineers": "West Virginia",
    "Jayhawks": "Kansas",
    "Terrapins": "Maryland",
    "Terps": "Maryland",
    "Boilermakers": "Purdue",
    "Hoosiers": "Indiana",
    "Illini": "Illinois",
    "Spartans": "Michigan State",
    "Scarlet Knights": "Rutgers",
    "Wolfpack": "NC State",
    "Aztecs": "San Diego State",
}


def _index_teams() -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for code, names in _NFL_TEAM_NAMES.items():
        for first in names:
            for name in (first, *(f"{first} {second}" for second in names if second != first)):
                index.setdefault(_affiliation_key(name), set()).add(code)
    return {key: frozenset(codes) for key, codes in index.items()}


_TEAMS_BY_KEY = _index_teams()
_COLLEGE_BY_NICKNAME = {_affiliation_key(nick): _affiliation_key(school) for nick, school in _COLLEGE_NICKNAMES.items()}

_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _mentions(text: str, names: Iterable[str]) -> list[tuple[int, int, frozenset[str]]]:
    """Every span of folded *text* naming someone in *names*: a full name owns
    itself, a bare last word owns every name ending in it."""
    full: list[tuple[int, int, frozenset[str]]] = []
    by_last: dict[str, set[str]] = {}
    for name in names:
        folded = _fold(name)
        tokens = [token for token in folded.split() if token.strip(".,") not in _NAME_SUFFIXES]
        if not tokens:
            continue
        pattern = rf"(?<!\w){re.escape(folded)}(?!\w)"
        full.extend((m.start(), m.end(), frozenset({name})) for m in re.finditer(pattern, text))
        by_last.setdefault(tokens[-1], set()).add(name)
    spans = list(full)
    for last, owners in by_last.items():
        for m in re.finditer(rf"(?<!\w){re.escape(last)}(?!\w)", text):
            if not any(start <= m.start() < end for start, end, _ in full):
                spans.append((m.start(), m.end(), frozenset(owners)))
    return spans


_CLAUSE_BREAK = re.compile("[;:—–]")
_CONSENSUS_WORDS = frozenset({"consensus", "adp"})
#: Nouns that make "… at 1.07" a consensus figure ("his consensus mark at 1.07")
#: rather than the slot a player went at ("three spots ahead of consensus at 1.04").
_CONSENSUS_NOUNS = frozenset(
    {"mark", "label", "tag", "line", "number", "projection", "slot", "spot", "perch", "rating", "value", "position"}
)
#: A value followed by one of these introduces the player named after it: "shut the
#: door at 1.12 by grabbing Missouri receiver Luther Burden". Never "with": "Ward at
#: 1.05 with Colorado quarterback Shedeur Sanders" is a pairing, and read as an object
#: it refuted a true sentence live (2026-09-14).
_OBJECT_CUE = re.compile(
    r"\s*(?:by|on|for|taking|grabbing|drafting|selecting|landing|snagging"
    r"|to (?:take|grab|draft|select|land|snag))\b"
)
#: Clause-break punctuation this close to a name is heading punctuation ("Gridiron
#: Gophers: A", "Pumas — Grade: A+"), not the start of a new clause.
_HEADING_GAP = 12


def _clause_break(gap: str) -> bool:
    return len(gap) > _HEADING_GAP and bool(_CLAUSE_BREAK.search(gap))


def _consensus_mark(text: str, start: int, end: int, owner_end: int | None) -> bool:
    """Whether the number at ``text[start:end]`` is a consensus mark ("his 1.04
    consensus mark", "consensus 1.05", "1.02 (consensus 1.01)") rather than where
    the player went. "… at 1.04" is a slot unless a consensus noun comes right
    before the "at"."""
    words = [word.strip("(") for word in re.findall(r"[\w(]+", text[max(0, start - 40) : start])]
    if words and words[-1] in {"at", "pick"} and (len(words) < 2 or words[-2] not in _CONSENSUS_NOUNS):
        return False
    before = set(words[-3:])
    between = set(re.findall(r"\w+", text[owner_end:start])) if owner_end is not None else set()
    after = re.match(r"\s*(\w+)", text[end:])
    return bool(_CONSENSUS_WORDS & (before | between)) or (after is not None and after.group(1) in _CONSENSUS_WORDS)


def _attributed(
    pattern: str, owner: str, sentence: str, names: Iterable[str], *, modifier: bool = False, value: bool = False
) -> bool:
    """Whether *pattern* occurs in *sentence* as *owner*'s. A value ("at 1.01")
    belongs to the name before it and a modifier ("Carolina back") to the name
    right after it, each falling back to the other side within its clause; a
    ``value`` next to "consensus" is nobody's pick. Measured misreadings this
    stops: "Jeanty at 1.01 over Omarion Hampton" as Hampton's slot, and "Sanders
    at 3.05, Stanford receiver Tre Harris" as Sanders's school. A value that
    cannot be tied to its claimed owner is no evidence against them."""
    text = _fold(sentence)
    spans = _mentions(text, names)
    only_owner = frozenset({owner})
    for match in re.finditer(pattern, text):
        before = max((s for s in spans if s[1] <= match.start()), key=lambda s: s[1], default=None)
        after = min((s for s in spans if s[0] >= match.end()), key=lambda s: s[0], default=None)
        before_ok = before is not None and not _clause_break(text[before[1] : match.start()])
        after_gap = text[match.end() : after[0]] if after is not None else ""
        after_ok = after is not None and not _clause_break(after_gap)
        tight_after = after_ok and len(after_gap) <= 40 and not re.search(r"[,()]", after_gap)
        if value and _consensus_mark(
            text, match.start(), match.end(), before[1] if before is not None and before_ok else None
        ):
            continue
        if modifier:
            chosen = after if tight_after else (before if before_ok else None)
        elif value and tight_after and _OBJECT_CUE.match(after_gap):
            chosen = after
        else:
            chosen = before if before_ok else (after if after_ok else None)
        if chosen is not None and chosen[2] == only_owner:
            return True
    return False


def _text_pattern(written: str) -> str:
    return rf"(?<!\w){re.escape(_fold(written))}(?:e?s)?(?!\w)"


def _label_pattern(label: tuple[int, int]) -> str:
    return rf"(?<![\d.]){label[0]}\.0?{label[1]}(?!\d)"


_ONES = (
    "",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _number_pattern(number: int) -> str:
    """A pick number as digits, or spelled out after a word that makes it a pick
    ("pick seventy-two"). A bare spelled number is left alone: "three spots
    early" is a margin, never pick three."""
    digits = rf"(?<![\d.]){number}(?!\d|\.\d)"
    if 0 < number < 20:
        words = _ONES[number]
    elif 20 <= number < 100:
        tens, ones = divmod(number, 10)
        words = _TENS[tens] + (f"[- ]{_ONES[ones]}" if ones else "")
    else:
        return digits
    return rf"{digits}|(?<![\w-])(?:pick|no\.?|number|selection) {words}(?![\w-])"


def _grade_pattern(grade: str) -> str:
    return rf"(?<!\w){re.escape(grade.strip().casefold())}(?![\w+-])"


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


def _judge_pick(claim: ExtractedClaim, narration: Narration, sentence: str) -> list[_Check]:
    player = _find_player(claim.player, narration)
    if player is None:
        return _unverifiable(f"player {claim.player!r} is not uniquely in the payload")
    names = [p.name for p in narration.players]
    checks: list[_Check] = []
    if (
        claim.pick_no is not None
        and player.pick_no is not None
        and _attributed(_number_pattern(claim.pick_no), player.name, sentence, names, value=True)
    ):
        checks.append(
            _compare("pick", player.name, claim.pick_no, player.pick_no, same=claim.pick_no == player.pick_no)
        )
    claimed_label, actual_label = _label_key(claim.board_label), _label_key(player.board_label)
    if (
        claimed_label is not None
        and actual_label is not None
        and _attributed(_label_pattern(claimed_label), player.name, sentence, names, value=True)
    ):
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
    return checks or _unverifiable(f"{player.name}: no pick detail the payload can check is tied to them")


def _judge_affiliation(claim: ExtractedClaim, narration: Narration, sentence: str) -> list[_Check]:
    """A school, NFL team, place or nickname attached to a player. Supported when it
    names either the player's college or their NFL team; refuted only when every
    reading of it contradicts the payload; unverifiable otherwise. Colleges are
    absent on the live path by design, and not knowing one never refutes one."""
    player = _find_player(claim.player, narration)
    written = claim.affiliation or claim.college
    if player is None and written and claim.player:
        swapped = _find_player(written, narration)
        if swapped is not None:  # the extractor wrote the two fields in the wrong order
            player, written = swapped, claim.player
    if player is None:
        return _unverifiable(f"player {claim.player!r} is not uniquely in the payload")
    if not written:
        return _unverifiable(f"{player.name}: no school or team stated")
    if not _attributed(
        _text_pattern(written), player.name, sentence, [p.name for p in narration.players], modifier=True
    ):
        return _unverifiable(f"{player.name}: {written!r} is not tied to them in the sentence")
    key = _affiliation_key(written)
    colleges = {_affiliation_key(p.college) for p in narration.players if p.college}
    own_college = _affiliation_key(player.college) if player.college else None
    readings: list[bool | None] = []
    teams = _TEAMS_BY_KEY.get(key)
    if teams:
        readings.append(player.nfl_team.upper() in teams if player.nfl_team else None)
    nickname = _COLLEGE_BY_NICKNAME.get(key)
    if nickname is not None:
        if own_college is None:
            readings.append(None)
        elif nickname == own_college:
            readings.append(True)
        else:
            readings.append(False if nickname in colleges else None)
    elif key in colleges:
        readings.append(None if own_college is None else key == own_college)
    elif key and any(college.endswith(f" {key}") for college in colleges):
        readings.append(None if own_college is None else own_college.endswith(f" {key}"))
    if not readings:
        return _unverifiable(f"{player.name}: {written!r} is no school or NFL team the payload can check")
    actual = f"college {player.college!r}, NFL team {player.nfl_team!r}"
    if any(reading is True for reading in readings):
        return [("supported", f"{player.name}: {written!r} matches the payload ({actual})", written)]
    if all(reading is False for reading in readings):
        return [("refuted", f"{player.name}: affiliation claimed {written!r}, payload has {actual}", written)]
    return _unverifiable(f"{player.name}: {written!r} cannot be checked against the payload ({actual})")


def _team_or_unverifiable(claim: ExtractedClaim, narration: Narration) -> NarrationTeam | None:
    return _find_team(claim.manager, narration)


def _judge_grade(claim: ExtractedClaim, narration: Narration, sentence: str) -> list[_Check]:
    team = _team_or_unverifiable(claim, narration)
    claimed = _grade_letter(claim.grade)
    if team is None or claimed is None or not team.manager:
        return _unverifiable(f"grade claim for {claim.manager!r} is not checkable")
    managers = [t.manager for t in narration.teams if t.manager]
    if not _attributed(_grade_pattern(claim.grade or ""), team.manager, sentence, managers):
        return _unverifiable(f"{team.manager}: the grade {claim.grade!r} is not tied to them in the sentence")
    actual = _grade_letter(team.grade)
    if actual is None:
        return _unverifiable(f"{team.manager}: payload grade {team.grade!r} is not a letter grade")
    # The letter decides it. "an A" for an A+ is loose phrasing, not an invented
    # grade, and a sentence is not worth excising over the modifier.
    return [_compare("grade", team.manager, claim.grade, team.grade, same=claimed == actual)]


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


_JUDGES: dict[str, Callable[[ExtractedClaim, Narration, str], list[_Check]]] = {
    "pick": _judge_pick,
    "affiliation": _judge_affiliation,
    "college": _judge_affiliation,
    "grade": _judge_grade,
    "pick_count": lambda claim, narration, _sentence: _judge_pick_count(claim, narration),
    "position_count": lambda claim, narration, _sentence: _judge_position_count(claim, narration),
    "consensus": lambda claim, narration, _sentence: _judge_consensus(claim, narration),
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
    name = claim.player if claim.kind in ("pick", "affiliation", "college", "consensus") else claim.manager
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
        for v in (
            claim.college,
            claim.affiliation,
            claim.grade,
            claim.board_label,
            claim.consensus_label,
            claim.pick_no,
            claim.count,
        )
        if v is not None
    ]
    for i in candidates:
        if any(value and value in folded[i] for value in values):
            return sentences[i]
    return sentences[candidates[0]]


def _locate_numbered(claim: ExtractedClaim, sentences: tuple[str, ...], folded: tuple[str, ...]) -> str | None:
    """The numbered sentence, when it exists and names the claim's player or team.
    A miscounted number is ``None`` (unverifiable), never an innocent sentence
    handed to the repair tier."""
    index = (claim.sentence_no or 0) - 1
    if not 0 <= index < len(sentences):
        return None
    anchor = _anchor(claim)
    if anchor is not None and anchor not in folded[index]:
        return None
    return sentences[index]


_POSITION_WORDS_RE = (
    r"(?:running back|back|rusher|runner|tailback|wide receiver|receiver|wideout|pass-catcher"
    r"|tight end|quarterback|passer|signal-caller|QB|RB|WR|TE)"
)
_LABEL_RE = r"\d{1,2}\.\d{2}"
_NOT_A_CONSENSUS_FIGURE = r"(?! consensus| mark| projection| label| tag| line| number| slot| perch| rating)"


def _pattern_claims(prose: str, narration: Narration) -> tuple[ExtractedClaim, ...]:
    """Claims a few fixed sentence shapes state outright, read with no model call:
    "Carolina back Trevor Etienne", "Jeanty at 1.01", "at 1.12 by grabbing Luther
    Burden", "Ayomanor … pick 38 (4.02)". They go through the same adjudicator as
    the extractor's claims, which they supplement: on 40 planted errors they
    caught 32 alone and lifted the cheap extractor from 33 to 38, with one
    refutation — a real writer error — across 60 shipped Issues (2026-09-14).
    Paired slots ("Judkins at 1.07 with Egbuka at 1.08") are skipped on purpose."""
    names = sorted({p.name for p in narration.players if p.name}, key=len, reverse=True)
    if not names:
        return ()
    places = (
        {p.college for p in narration.players if p.college}
        | set(_COLLEGE_NICKNAMES)
        | {name for group in _NFL_TEAM_NAMES.values() for name in group}
    )
    name_alt = "|".join(re.escape(name) for name in names)
    place_alt = "|".join(re.escape(place) for place in sorted(places, key=len, reverse=True))
    not_paired = rf"(?!\s*(?:and|&|with)\s+(?:{_LABEL_RE}|(?:\w+ )?(?:{name_alt})))"
    shapes = (
        (
            "affiliation",
            re.compile(
                rf"\b(?P<aff>{place_alt})(?:'s|’s|-bound)?\s+(?:{_POSITION_WORDS_RE}\s+)?(?P<name>{name_alt})\b"
            ),
        ),
        (
            "pick",
            re.compile(
                rf"\b(?P<name>{name_alt})\b(?:,? (?:the|a|an) [\w' -]{{1,30}},)? "
                rf"(?:at|with the|(?:went|going|landed|came off|off the board)(?: at)?) "
                rf"(?P<slot>{_LABEL_RE})\b{_NOT_A_CONSENSUS_FIGURE}{not_paired}"
            ),
        ),
        (
            "pick",
            re.compile(
                rf"\bat (?P<slot>{_LABEL_RE}),? (?:by |on |for |to )?"
                rf"(?:taking|grabbing|drafting|selecting|landing|snagging|securing|scooping(?: up)?|adding|calling)? ?"
                rf"(?:[\w' -]{{0,30}} )?(?P<name>{name_alt})\b(?!\s+(?:at|\()\s*{_LABEL_RE})"
            ),
        ),
        ("pick", re.compile(rf"\b(?P<name>{name_alt})\b[^.;:()]{{0,40}}\bpick \d{{1,2}} \((?P<slot>{_LABEL_RE})\)")),
    )
    claims: list[ExtractedClaim] = []
    for number, sentence in enumerate(_sentences(_normalize(prose)), start=1):
        for kind, shape in shapes:
            for match in shape.finditer(sentence):
                field = {"affiliation": match["aff"]} if kind == "affiliation" else {"board_label": match["slot"]}
                claims.append(
                    ExtractedClaim.model_validate({"kind": kind, "s": number, "player": match["name"], **field})
                )
    return tuple(claims)


def _claim_key(claim: ExtractedClaim) -> tuple[str, int | None, str, str]:
    kind = "affiliation" if claim.kind == "college" else claim.kind
    value = claim.affiliation or claim.college or claim.board_label or ""
    return (kind, claim.sentence_no, _name_key(claim.player or ""), _fold(value))


def number_sentences(prose: str) -> str:
    """*prose* as the extractor reads it: one ``[n] sentence`` line per sentence,
    split exactly as :func:`adjudicate` splits it, so a claim's ``s`` names the
    same sentence on both sides."""
    return "\n".join(f"[{n}] {text}" for n, text in enumerate(_sentences(_normalize(prose)), start=1))


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
        located = (
            _locate(claim, sentences, folded)
            if claim.sentence_no is None
            else _locate_numbered(claim, sentences, folded)
        )
        if located is None:
            verdicts.append(
                ClaimVerdict(claim, "unverifiable", "the claim's sentence was not found in the prose", None, "")
            )
            continue
        status, reason, matched = _combine(_JUDGES[claim.kind](claim, narration, located))
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
            if verdict.claim.kind not in GATING_KINDS:
                continue
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
            number_sentences(prose),
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
    seen = {_claim_key(claim) for claim in claims}
    extra = tuple(claim for claim in _pattern_claims(prose, narration) if _claim_key(claim) not in seen)
    return VerificationResult(ran=True, verdicts=adjudicate(claims + extra, prose, narration), unparsed=unparsed)
