"""Stage 4 — deterministic content-safety checks over a narrator's output (AD-12 L2).

``check_narration(text, narration, *, voice=None)`` runs a fixed, credential-free
pipeline over the prose *any* narrator produced — the template narrator, the LLM
narrator, or the demo path alike — and returns a :class:`SafetyReport` of
:class:`SafetyFinding`\\ s. It **returns findings; it does not act** —
``narrate/response.py`` (AD-12 Layer 3) turns the report into a graded decision
(suppress the offending section / one LLM regeneration / hold the whole Issue)
and ``cli.py`` applies it alongside narrator *selection*.

Import fence (AD-4 / I4): this module imports **stdlib +
``commishdesk.facts.schema`` + ``commishdesk.voices`` + ``commishdesk.errors``
only** — no provider SDK, no ``httpx``, no network, no clock, no
``commishdesk.ingest``. It is re-exported *eagerly* from ``narrate/__init__.py``
(like ``template.py``), so ``import commishdesk.narrate`` still pulls in no SDK.

Pure and deterministic: the same ``(text, narration, voice)`` yields a
byte-identical report with a stable ``findings`` order — findings are emitted in
the fixed check order below, and within a check in sentence order then match
order, de-duplicated.

Fixed order (AD-12 Layer 2):

1. NFKC-normalize the text and strip ``Cc`` / ``Cf`` code points (keep newlines),
   then mask the league's own name out of the *working copy* (see
   :func:`_mask_league_name`) so a league called "The Sportsbook League" cannot
   brick its own Issue.
2. **Named-person proximity** — a sentence carrying a manager's name (from the
   ``narration`` projection) *and* a banned-category term *or* a personal-insult
   hit → ``named_person_proximity`` / ``hold_issue``.
3. **Banned-topic patterns** — the same term with no manager name in that
   sentence → the lesser ``banned_topic`` tier.
4. **Closed-world** — every proper-noun / numeric token in the output must be a
   *member* of the token set built from ``narration.model_dump_json()`` (minus a
   curated stop-set) — exact membership, never substring containment. A miss →
   ``hallucination`` / ``regenerate``. This is the package's own gate against
   gross hallucination, not a proof of accuracy; ``tests/test_voices.py``'s eval
   scorer now *calls* :func:`_closed_world` rather than carrying a copy of it.
5. **Slop / tone** — a cliché from the ``slop`` list → ``slop``.

Pattern lists live in ``commishdesk/narrate/safety_lists.toml`` (shipped as
package data, loaded via :mod:`importlib.resources` + :mod:`tomllib`) so they are
editable without a code change; a load/compile failure raises
:class:`~commishdesk.errors.NarratorError`. A ``Voice``'s ``banned_topics`` prose
merges in on top as extracted keyword patterns under a ``voice:<voice_id>``
category.
"""

from __future__ import annotations

import functools
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from commishdesk.errors import NarratorError

if TYPE_CHECKING:
    from commishdesk.facts.schema import Narration
    from commishdesk.voices import Voice

__all__ = [
    "CATEGORY_SEVERITY",
    "SafetyCategory",
    "SafetyFinding",
    "SafetyReport",
    "SafetySeverity",
    "check_narration",
    "closed_world_tokens",
]

SafetyCategory = Literal[
    "named_person_proximity", "banned_topic", "hallucination", "slop"
]
SafetySeverity = Literal["hold_issue", "regenerate", "suppress_section"]

#: The AD-12 tier map: each category's severity, stamped onto every
#: :class:`SafetyFinding` at construction. ``narrate/response.py`` reads
#: ``finding.severity`` to grade its response — ``hold_issue`` holds the whole
#: Issue, ``regenerate`` earns one LLM regeneration, ``suppress_section`` drops
#: the offending section (or degrades LLM prose to the template).
CATEGORY_SEVERITY: dict[SafetyCategory, SafetySeverity] = {
    "named_person_proximity": "hold_issue",
    "banned_topic": "suppress_section",
    "hallucination": "regenerate",
    "slop": "suppress_section",
}

_LISTS_FILENAME = "safety_lists.toml"


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SafetyFinding(_Frozen):
    """One issue the check raised, with the sentence and the matched text."""

    category: SafetyCategory
    severity: SafetySeverity
    message: str
    sentence: str
    matched: str


class SafetyReport(_Frozen):
    """The verdict on one narration: an ordered, de-duplicated finding tuple."""

    findings: tuple[SafetyFinding, ...] = ()

    @property
    def ok(self) -> bool:
        """No finding of any kind."""
        return not self.findings

    @property
    def held(self) -> bool:
        """At least one ``hold_issue`` finding. Convenience predicate only —
        ``narrate/response.py``'s :func:`classify` is the authority on what a
        report means (it can, for one, ignore a template narrator's
        false-positive ``hallucination``)."""
        return any(f.severity == "hold_issue" for f in self.findings)


# --------------------------------------------------------------------------- #
# (1) normalize
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    """NFKC-normalize, then drop every ``Cc`` / ``Cf`` code point except ``\\n``.

    This runs first so a zero-width / soft-hyphen evasion ("M\\u200ba\\u200brcus")
    collapses to plain text before any name or pattern match.

    Deletion, not substitution: a zero-width character *between* two separate
    words therefore joins them ("drafted\\u200bhungover" -> "draftedhungover"),
    and homoglyph / confusable substitution ("М" U+041C for "M") is not handled
    at all — NFKC does not fold those. This check is a deterministic floor; the
    Epic-7 Layer-4 classifier is the answer to adversarial evasion, not this
    normalizer.
    """
    nfkc = unicodedata.normalize("NFKC", text)
    return "".join(
        ch
        for ch in nfkc
        if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf")
    )


#: Split on sentence punctuation followed by whitespace *or* an uppercase letter
#: (so a missing space, "hungover.He", still splits), plus any newline run. Biased
#: toward over-splitting: a false split only downgrades a would-be proximity hold
#: to two lesser findings, while a false merge fabricates a whole-league hold.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])(?=\s|[A-Z])|\n+")


def _sentences(text: str) -> tuple[str, ...]:
    """Split normalized text into non-empty, stripped sentence-ish spans."""
    return tuple(p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip())


#: Shortest ``league.name`` worth masking. Below this a name is far too likely to
#: be a bare initialism that collides with ordinary prose ("GO", "TW"), and the
#: false-positive it could cause is cheaper than the false-negative masking it
#: would create.
_LEAGUE_NAME_MIN = 3

#: The inert stand-in a masked league name is replaced with. Both words are in
#: ``_STOP`` / ``_VOICE_STOPWORDS`` territory and neither trips any pattern list,
#: so substituting it can only ever *remove* findings, never add one.
_LEAGUE_PLACEHOLDER = "the league"


def _league_name_pattern(name: str) -> re.Pattern[str] | None:
    """Compile the whole-token, whitespace-flexible pattern that matches *name* in
    already-``_normalize``\\ d text, or ``None`` when the name must not be masked.

    Three details the naive ``rf"\\b{re.escape(name)}\\b"`` got wrong:

    * ``\\b`` only anchors *next to a word character*, so a name whose first or
      last character is punctuation or an emoji ("Sportsbook!", "(Sportsbook)",
      "🏈 Politics League") never matched and the brick came straight back. The
      edge anchors are chosen per-character: ``\\b`` beside a word character,
      a non-space lookaround otherwise.
    * The pattern is built from ``_normalize(name)``, because the *text* it runs
      against has already been normalized — a full-width "Ｓportsbook" in the
      league name folds to "Sportsbook" in the prose and would never have matched
      its own raw form.
    * The name's words are joined with ``\\s+``, so a line wrap or a double space
      between them in the prose still matches.

    ``None`` (do not mask) for: a name shorter than :data:`_LEAGUE_NAME_MIN`, and
    a *single-word* name that is an ordinary English word — a league called "The"
    or "Run" must not rewrite every occurrence of that word in the recap.
    """
    normalized = _normalize(name).strip()
    if len(normalized) < _LEAGUE_NAME_MIN:
        return None
    words = normalized.split()
    if not words:
        return None
    if len(words) == 1 and words[0].lower() in _STOP | _VOICE_STOPWORDS:
        return None

    def _edge(char: str, *, leading: bool) -> str:
        if char.isalnum() or char == "_":
            return r"\b"
        return r"(?<!\S)" if leading else r"(?!\S)"

    body = r"\s+".join(re.escape(word) for word in words)
    return re.compile(
        _edge(normalized[0], leading=True) + body + _edge(normalized[-1], leading=False),
        re.IGNORECASE,
    )


def _mask_league_name(
    text: str, narration: Narration, *, manager_names: frozenset[str] | None = None
) -> str:
    """Replace whole-token, case-insensitive occurrences of the league's own name
    with :data:`_LEAGUE_PLACEHOLDER` — in ``check_narration``'s **internal
    working copy only**.

    The league name is a league-supplied free string that reaches every Issue's
    title and dateline on every run (manager and team display names are equally
    league-supplied, but they appear only where the narrator chose to put them,
    and masking them would gut the named-person proximity check outright).
    ``ingest/sanitize.py`` (AD-24) already scrubbed the name structurally.
    Without this mask, a league called "The Sportsbook League" or "Politics
    League" trips a curated pattern on *every* run and can never produce an Issue
    on the zero-credential template path — a permanent brick with no operator
    recourse.

    Neutralising the name in the working text kills that false positive across
    all five checks at once, and the reader still sees the real name everywhere
    (``render_draft_recap`` / ``recap_to_text`` / the rendered HTML are
    untouched; findings report the *unmasked* sentence — see
    :func:`check_narration`). Accepted loss: if a word of the name is also a
    banned term and the narrator *separately* misuses that exact name string, the
    body hit is masked too. A standalone use of the word is **not** masked (only
    the full name string is), the LLM system prompt bans these topics at Layer 1,
    the Epic-7 classifier is the real answer to evasion, and
    ``--allow-content-hold`` is the operator backstop for every other hold.

    Masking is skipped entirely when the league name **contains a manager's
    name** ("Marcus Memorial League"): stripping it would take the manager's name
    out of the sentence before the proximity check ever sees it, silently
    downgrading a genuine ``hold_issue``. A false positive on such a league is
    the operator's problem to override; a missed hold is not.
    """
    pattern = _league_name_pattern(narration.league.name)
    if pattern is None:
        return text
    names = _manager_names(narration) if manager_names is None else manager_names
    normalized_name = _normalize(narration.league.name)
    if any(
        name_pattern.search(normalized_name)
        for name_pattern in _compile_name_patterns(names)
    ):
        return text
    return pattern.sub(_LEAGUE_PLACEHOLDER, text)


# --------------------------------------------------------------------------- #
# (2) manager names from the narration projection
# --------------------------------------------------------------------------- #


def _manager_names(narration: Narration) -> frozenset[str]:
    """Every manager name reachable in the ``narration`` projection — the
    ``manager`` field wherever it appears, plus the ``left_waiting`` name lists.
    Single-character names are dropped; two-character handles ("AJ", "TJ", "Bo")
    are kept — word-boundary matching (see :func:`_compile_name_patterns`) makes
    them safe."""
    names: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "manager" and isinstance(value, str) and value.strip():
                    names.add(value.strip())
                elif key == "left_waiting" and isinstance(value, list):
                    names.update(
                        item.strip()
                        for item in value
                        if isinstance(item, str) and item.strip()
                    )
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(narration.model_dump())
    return frozenset(name for name in names if len(name) >= 2)


def _compile_name_patterns(names: frozenset[str]) -> tuple[re.Pattern[str], ...]:
    """One ``\\b``-anchored, case-insensitive pattern per manager name — compiled
    once per :func:`check_narration` call. Word-boundary, never bare substring:
    "Sam" must not fire inside "same", "Ben" inside "benched", "Ross" inside
    "across"."""
    return tuple(
        re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE) for name in sorted(names)
    )


def _sentence_has_name(sentence: str, name_patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(sentence) for pattern in name_patterns)


# --------------------------------------------------------------------------- #
# lists — TOML package data, loaded + compiled once
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Lists:
    banned_topics: dict[str, tuple[re.Pattern[str], ...]]
    personal_insults: tuple[re.Pattern[str], ...]
    slop: tuple[re.Pattern[str], ...]


def _compile_all(patterns: object, where: str) -> tuple[re.Pattern[str], ...]:
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise NarratorError(f"{_LISTS_FILENAME}: {where} must be a list of strings")
    try:
        return tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    except re.error as exc:
        raise NarratorError(
            f"{_LISTS_FILENAME}: {where} has an uncompilable pattern ({exc})"
        ) from exc


def _parse_lists(text: str) -> _Lists:
    """Parse + compile the raw TOML. Any failure → :class:`NarratorError`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise NarratorError(f"{_LISTS_FILENAME} is not valid TOML ({exc})") from exc

    raw_banned = data.get("banned_topics", {})
    if not isinstance(raw_banned, dict):
        raise NarratorError(f"{_LISTS_FILENAME}: [banned_topics] must be a table")
    banned = {
        str(category): _compile_all(patterns, f"banned_topics.{category}")
        for category, patterns in raw_banned.items()
    }
    return _Lists(
        banned_topics=banned,
        personal_insults=_compile_all(
            data.get("personal_insults", []), "personal_insults"
        ),
        slop=_compile_all(data.get("slop", []), "slop"),
    )


def _lists_toml_text() -> str:
    return (
        resources.files("commishdesk.narrate")
        .joinpath(_LISTS_FILENAME)
        .read_text(encoding="utf-8")
    )


@functools.lru_cache(maxsize=1)
def _load_lists() -> _Lists:
    """Load + compile the packaged list file. A missing / non-UTF-8 file surfaces
    as :class:`NarratorError` (chained) rather than an ``OSError`` /
    ``UnicodeDecodeError`` that would escape the CLI's per-league catch and abort
    the whole batch (AD-9)."""
    try:
        text = _lists_toml_text()
    except (OSError, UnicodeDecodeError, ModuleNotFoundError) as exc:
        raise NarratorError(
            f"{_LISTS_FILENAME} could not be read ({type(exc).__name__})"
        ) from exc
    return _parse_lists(text)


# --------------------------------------------------------------------------- #
# voice keyword merge
# --------------------------------------------------------------------------- #

#: One word of a ``banned_topics`` phrase, carrying its possessive marker so
#: "player's" is read as one owner token and not as "player" + a stray "s".
_VOICE_WORD = re.compile(r"[a-z]+(?:['’]s)?")

#: The possessive marker on such a word.
_VOICE_POSSESSIVE = re.compile(r"['’]s$")

#: Split a ``banned_topics`` phrase into the independent topics it enumerates:
#: commas, semicolons, and the connectives "or" / "and". "gambling lines or
#: betting advice" is two topics, not one seven-word phrase that matches nothing.
_VOICE_SPLIT = re.compile(r"[,;]|\bor\b|\band\b")

#: Leading determiners and possessives stripped off a fragment before it becomes
#: a pattern — "a player's real-life injury history" is about injury history, and
#: the "a player's" prefix would stop the phrase ever matching real copy. Any
#: leading token ending in ``'s`` / ``’s`` is treated as a possessive too.
_VOICE_LEADING = frozenset({"a", "an", "the", "his", "her", "its", "their", "our", "your"})

#: Words never worth being a voice pattern's *only* content — generic
#: fantasy-football / sentence scaffolding. A fragment made up entirely of these
#: contributes nothing; the curated lists in ``safety_lists.toml`` own the
#: single-term topics ("politics", "religion", "nationality", "injury", …).
#: A voice-keyword hit can *only* ever produce a ``banned_topic`` (warn) finding
#: — never a ``hold_issue``, even beside a manager name (see
#: :func:`check_narration`) — so this set only needs to keep warn-tier noise (and
#: the demo recap) down, not to be exhaustive.
_VOICE_STOPWORDS: frozenset[str] = frozenset(
    {
        "your", "their", "them", "they", "this", "that", "these", "those",
        "with", "from", "about", "over", "into", "than", "then", "when",
        "where", "have", "been", "being", "other", "around", "during",
        "real", "life",
        "player", "players", "manager", "managers", "roster", "rosters",
        "team", "teams", "draft", "pick", "picks", "league", "season",
        "field", "line", "lines", "history", "status", "advice",
    }
)

#: The minimum number of words a fragment must carry to become a pattern. One
#: word is never enough: a bare word lifted out of a Voice's prose ("injury",
#: "physical", "trouble") re-arms exactly the terms ``safety_lists.toml``
#: deliberately excluded, and fires on ordinary scouting talk ("took the
#: injury-prone back", "the physical tools are there").
_VOICE_MIN_WORDS = 2

#: What separates two words of a voice phrase in real copy: whitespace or a
#: hyphen ("day-trading habit" must match "day trading habit" and vice versa).
_VOICE_GAP = r"[\s-]+"


def _voice_fragments(topics: frozenset[str]) -> list[str]:
    """Split every ``banned_topics`` phrase into multi-word fragments, in a stable
    order, de-duplicated.

    Each phrase is lowercased and split on commas / semicolons / "or" / "and";
    each fragment is reduced to its word tokens, leading determiners and
    possessives are dropped, and a fragment is kept only when it still carries at
    least :data:`_VOICE_MIN_WORDS` words *and* at least one word outside
    :data:`_VOICE_STOPWORDS`.

    A fragment longer than two words also contributes its **trailing two-word
    head-noun core**: "a player's real-life injury history" arms both the full
    string and "injury history", "off-field legal trouble" arms "legal trouble".
    Without that, the long form is a dead pattern — it only fires if the narrator
    quotes the voice's own prose verbatim, which no narrator does.
    """
    fragments: list[str] = []
    seen: set[str] = set()

    def _add(words: list[str]) -> None:
        if len(words) < _VOICE_MIN_WORDS:
            return
        if all(word in _VOICE_STOPWORDS for word in words):
            return
        key = " ".join(words)
        if key not in seen:
            seen.add(key)
            fragments.append(key)

    for phrase in sorted(topics):
        for chunk in _VOICE_SPLIT.split(phrase.lower()):
            # (word, was_possessive) — "player's" -> ("player", True)
            marked = [
                (_VOICE_POSSESSIVE.sub("", raw), bool(_VOICE_POSSESSIVE.search(raw)))
                for raw in _VOICE_WORD.findall(chunk)
            ]
            # drop leading determiners and possessive owners ("a player's ...")
            while marked and (marked[0][0] in _VOICE_LEADING or marked[0][1]):
                marked.pop(0)
            words = [word for word, _ in marked if word]
            _add(words)
            if len(words) > _VOICE_MIN_WORDS:
                _add(words[-_VOICE_MIN_WORDS:])
    return fragments


def _voice_patterns(voice: Voice | None) -> dict[str, tuple[re.Pattern[str], ...]]:
    """Extract **multi-word phrase** patterns from ``voice.banned_topics`` prose,
    compiled under a synthetic ``voice:<voice_id>`` category. Empty
    ``frozenset`` / ``voice=None`` → ``{}`` (base lists only).

    Never bare single words. A ``Voice``'s ``banned_topics`` are prose ("a
    player's real-life injury history or medical status"), and lifting single
    words out of them re-arms the exact terms ``safety_lists.toml`` deliberately
    keeps out of its own lists — "injury", "physical", "trouble" — which then
    fire on ordinary football copy. Phrases ("injury history", "betting advice",
    "medical status") are what a voice actually means, and they duplicate already
    safe curated patterns rather than fighting them. Single-word topics
    ("politics", "religion", "nationality") contribute nothing: the curated
    ``politics_religion`` category already owns them.

    :func:`check_narration` treats a ``voice:`` category as warn-tier only — it
    never contributes a ``hold_issue``, even beside a manager name."""
    if voice is None:
        return {}
    topics: frozenset[str] = voice.banned_topics
    if not topics:
        return {}

    fragments = _voice_fragments(topics)
    if not fragments:
        return {}

    category = f"voice:{voice.voice_id or 'voice'}"
    return {
        category: tuple(
            re.compile(
                r"\b" + _VOICE_GAP.join(re.escape(w) for w in fragment.split(" ")) + r"\b",
                re.IGNORECASE,
            )
            for fragment in fragments
        )
    }


# --------------------------------------------------------------------------- #
# (4) closed-world — the package's own gate; tests/test_voices.py calls into it
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.’'%$+/-]*")

#: A letter-grade token (``A`` / ``B+`` / ``d-``). Checked against the grades
#: actually awarded *before* the generic strip runs — otherwise ``A+`` / ``B+``
#: collapse to ``a`` / ``b`` and a hallucinated grade sails through.
#: Both cases are matched: a lowercase ``a+`` is the same claim as ``A+`` and
#: must not bypass the check by falling through the "is it capitalised?" filter
#: below. The class is spelled out rather than written ``[A-F]`` + ``IGNORECASE``
#: because ``A-F`` includes ``E``, which is not a grade on this scale — a bare
#: lowercase "e" would then be routed into the grade branch and reported as a
#: hallucination.
_GRADE = re.compile(r"^[ABCDFabcdf][+-]?$")

#: A trailing possessive ``'s`` / ``’s`` — dropped before the closed-world
#: lookup so the template narrator's own "Blitz Alpacas's swing" is not read as
#: an out-of-world token (Story 3.4 calibration).
_POSSESSIVE = re.compile(r"['’]s$")

#: A trailing ordinal suffix on a number ("11th" -> "11", "3rd" -> "3") — dropped
#: before the payload lookup so an ordinal phrasing of a real number is not read
#: as a hallucinated one.
_ORDINAL = re.compile(r"(?<=\d)(st|nd|rd|th)$", re.IGNORECASE)

#: Scaffolding / section-heading words that are capitalised or numeric in prose
#: but are not proper nouns or facts to trace. Ported verbatim from
#: ``tests/test_voices.py::_score_recap`` and then extended (clearly marked) with
#: the capitalised scaffolding the *template* narrator emits in its own prose.
_STOP: frozenset[str] = frozenset(
    {
        # articles / conjunctions / prepositions / pronouns / demonstratives
        "a", "an", "and", "or", "but", "nor", "so", "yet", "for", "of", "in", "on",
        "at", "to", "by", "as", "with", "from", "into", "than", "then", "that",
        "this", "these", "those", "there", "their", "them", "they", "it", "its",
        "he", "his", "him", "she", "her", "we", "we'll", "us", "our", "you",
        "your", "i", "if", "is", "was", "were", "be", "been", "being",
        "not", "no",
        # common sentence-openers / adverbs
        "the", "now", "here", "how", "when", "where", "what", "who", "whom",
        "which", "while", "after", "before", "once", "still", "also", "even",
        "just", "only", "both", "each", "every", "some", "any", "all", "most",
        "more", "less", "nobody", "everyone", "someone", "nothing", "everything",
        "because", "since", "until", "about", "over", "under", "between", "whether",
        # capitalised sentence-opener adverbs / conjuncts (Story 3.4)
        "meanwhile", "however", "granted", "remarkably", "elsewhere", "instead",
        "overall", "ultimately", "regardless", "admittedly", "notably",
        "curiously", "predictably", "otherwise", "besides", "conversely",
        "importantly", "frankly", "honestly", "arguably", "presumably",
        # section-heading / scaffolding words
        "round", "board", "lead", "superlatives", "grades", "grade", "team",
        "teams", "positional", "read", "picks", "pick", "december", "arguing",
        "best", "reach", "value", "swing", "swings", "boldest", "biggest",
        "runner-up", "consensus", "draft", "recap", "season", "waiting",
        "early", "window", "later", "run", "runs",
        # position nouns not spelled out in the narration projection
        "quarterback", "quarterbacks", "receiver", "receivers", "wideout",
        "wideouts", "tight", "flex", "kicker", "defense",
        # spelled integers used as plain scaffolding
        "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
        "hundred",
        # --- Story 3.4: capitalised scaffolding in the template narrator's prose
        "running", "back", "backs", "end", "ends",
    }
)


#: The bare single letters a grade-scale description names ("the scale runs A to
#: F") — always in-world regardless of who was graded. A *suffixed* grade
#: ("F-", "D+") must still be one actually awarded.
_GRADE_SCALE_LETTERS = frozenset("ABCDF")


def _split_token(raw: str) -> list[str]:
    """The token pieces one raw ``_TOKEN`` match contributes: trailing sentence
    punctuation and a possessive dropped, split on internal ``-`` / ``/``, each
    piece stripped of an ordinal suffix and of ``%$+/-`` padding.

    One helper, used for **both** sides of the closed-world comparison, so the
    prose and the payload are tokenised identically — "12-team" in either place
    yields ``["12", "team"]``, "4.5/PPR" yields ``["4.5", "ppr"]``, "11th"
    yields ``["11"]``."""
    trimmed = raw.strip(".,;:!?()[]\"'’")
    base = _POSSESSIVE.sub("", trimmed.strip("%$+/-"))
    parts = []
    for part in re.split(r"[-/]", base):
        token = _ORDINAL.sub("", part).strip("%$+/-")
        if token:
            parts.append(token)
    return parts


def _payload_tokens(narration: Narration) -> frozenset[str]:
    """The closed world: every token of ``narration.model_dump_json()``,
    lowercased and split by :func:`_split_token` exactly as the prose side is.

    This is a **set**, not a string, and membership below is exact. Substring
    containment (the pre-calibration behaviour) silently passed any prose token
    that happened to sit inside a payload one — "19" inside ``picks_total=192``,
    "Marc" inside "Marcus", "1.0" inside "1.01" — which is precisely the class of
    near-miss hallucination this check exists to catch.

    The payload is ``_normalize``\\ d first, exactly as the prose side is. Under
    exact membership that is load-bearing, not cosmetic: an NFKC-foldable
    character in a player or manager name ("Ｒomeo") folds in the prose but not
    in the raw JSON, and the two would never compare equal — a guaranteed false
    hallucination that substring matching used to paper over."""
    tokens: set[str] = set()
    for raw in _TOKEN.findall(_normalize(narration.model_dump_json()).lower()):
        tokens.update(_split_token(raw))
    return frozenset(tokens)


def _closed_world(text: str, narration: Narration) -> list[str]:
    """Every capitalised or numeric token in *text* (minus the stop-set) that is
    not a **member** of the payload's token set, order-stable and de-duplicated.

    A ``_GRADE``-shaped token is checked case-folded against the grades actually
    awarded (plus the bare scale letters), not against the payload token set — so
    a lowercase "a+" nobody earned is caught, and a lowercase "a+" that *was*
    awarded is not. A token is split on internal ``-`` / ``/`` and stripped of an
    ordinal suffix before the lookup so "12-team", "4.5/PPR" and "11th" do not
    flag."""
    payload = _payload_tokens(narration)
    awarded = {team.grade.upper() for team in narration.teams}
    unknown: list[str] = []
    for raw in _TOKEN.findall(text):
        trimmed = raw.strip(".,;:!?()[]\"'’")
        if _GRADE.match(trimmed):
            upper = trimmed.upper()
            if upper not in awarded and not (
                len(upper) == 1 and upper in _GRADE_SCALE_LETTERS
            ):
                unknown.append(trimmed)
            continue
        for token in _split_token(raw):
            if not (token[:1].isupper() or any(ch.isdigit() for ch in token)):
                continue
            folded = token.lower()
            if folded in _STOP or folded in payload:
                continue
            unknown.append(token)
    return list(dict.fromkeys(unknown))


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #


def _working_text(text: str, narration: Narration) -> str:
    """The exact string every check in :func:`check_narration` matches against:
    ``_normalize``\\ d, then league-name-masked. Shared with
    :func:`closed_world_tokens` so no caller can score a *different* string than
    the shipping gate does."""
    return _mask_league_name(_normalize(text), narration)


def closed_world_tokens(text: str, narration: Narration) -> tuple[str, ...]:
    """The out-of-world tokens :func:`check_narration`'s closed-world check would
    report for *text* — normalize, league-name mask, closed-world, in that order.

    The one public entry point for scoring prose against the Facts payload
    without running the whole check. ``tests/eval/voices``' scorer calls this
    rather than reimplementing (or partially reimplementing) the pipeline: the
    eval harness and the gate every Issue passes through must never be able to
    disagree about what is in-world.
    """
    return tuple(_closed_world(_working_text(text, narration), narration))


def _containing_sentence(pairs: tuple[tuple[str, str], ...], needle: str) -> str:
    """The **reported** (unmasked) sentence of the first ``(working, reported)``
    pair whose working half contains *needle*."""
    for working, reported in pairs:
        if needle in working:
            return reported
    return ""


def _sentence_pairs(working: str, normalized: str) -> tuple[tuple[str, str], ...]:
    """Pair each working (masked) sentence with the unmasked sentence to report.

    Findings must carry the sentence a *reader* would see: ``cli.py`` hands
    ``SafetyFinding.sentence`` to :func:`~commishdesk.narrate.response.suppress_sections`,
    which looks for it inside the recap's real (unmasked) section blocks. Report
    the masked sentence and that lookup misses, suppression finds no section, and
    the Issue is held — D1's own failure mode, reintroduced one layer down.

    Alignment is positional, so it is only trusted when both splits produce the
    same number of sentences. They can disagree: the placeholder is lowercase, so
    a league name whose text position drove a ``(?<=[.!?])(?=[A-Z])`` split
    ("…done.Sportsbook League won.") merges two working sentences into one. In
    that case the masked sentences are reported — a conservative, still-correct
    report that only costs section localization on a genuinely odd input.
    """
    masked = _sentences(working)
    raw = _sentences(normalized)
    if len(masked) != len(raw):
        raw = masked
    return tuple(zip(masked, raw, strict=True))


def check_narration(
    text: str, narration: Narration, *, voice: Voice | None = None
) -> SafetyReport:
    """Run the five checks in fixed order and return an ordered
    :class:`SafetyReport`. Pure, deterministic, credential-free.

    Every check *matches* against the working copy (normalized + league-name
    masked); every finding *reports* the corresponding unmasked sentence.

    Raises :class:`~commishdesk.errors.NarratorError` if
    ``safety_lists.toml`` cannot be loaded or a pattern will not compile.
    """
    lists = _load_lists()
    names = _manager_names(narration)
    name_patterns = _compile_name_patterns(names)
    # The working copy: normalized, then the league's own name masked out (D1).
    # Nothing downstream of this function sees it — render_draft_recap /
    # recap_to_text / the rendered HTML all still carry the real name, and so do
    # the sentences the findings report.
    normalized = _normalize(text)
    working = _mask_league_name(normalized, narration, manager_names=names)
    pairs = _sentence_pairs(working, normalized)

    # base categories first (TOML order), then the synthetic voice category
    categories: dict[str, tuple[re.Pattern[str], ...]] = {
        **lists.banned_topics,
        **_voice_patterns(voice),
    }

    # per-sentence hit data, gathered once: (reported, has_name, cats, insults)
    rows: list[tuple[str, bool, list[tuple[str, str]], list[str]]] = []
    for sentence, reported in pairs:
        cat_hits: list[tuple[str, str]] = []
        for category, patterns in categories.items():
            for pattern in patterns:
                match = pattern.search(sentence)
                if match:
                    cat_hits.append((category, match.group(0)))
        insult_hits = [
            match.group(0)
            for pattern in lists.personal_insults
            if (match := pattern.search(sentence))
        ]
        rows.append(
            (reported, _sentence_has_name(sentence, name_patterns), cat_hits, insult_hits)
        )

    findings: list[SafetyFinding] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(category: SafetyCategory, message: str, sentence: str, matched: str) -> None:
        key = (category, sentence, matched, message)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            SafetyFinding(
                category=category,
                severity=CATEGORY_SEVERITY[category],
                message=message,
                sentence=sentence,
                matched=matched,
            )
        )

    # (2) named-person proximity — every hold, in sentence order. A voice-derived
    # keyword category (``voice:<id>``) is skipped here: crude keyword extraction
    # over free-text prose is too false-positive-prone to silently hold a whole
    # league, so it only ever reaches the warn tier in step (3).
    for sentence, has_name, cat_hits, insult_hits in rows:
        if not has_name:
            continue
        for category, matched in cat_hits:
            if category.startswith("voice:"):
                continue
            add(
                "named_person_proximity",
                f"a manager's name and {category} content in one sentence: {matched!r}",
                sentence,
                matched,
            )
        for matched in insult_hits:
            add(
                "named_person_proximity",
                f"a manager's name and a personal insult in one sentence: {matched!r}",
                sentence,
                matched,
            )

    # (3) banned-topic patterns — the lesser (warn) tier. Every hit in a sentence
    # with no manager name lands here; in a *named* sentence, only the
    # voice-derived categories do (the curated categories already produced a hold
    # in step 2 and must not double-report).
    for sentence, has_name, cat_hits, insult_hits in rows:
        for category, matched in cat_hits:
            if has_name and not category.startswith("voice:"):
                continue
            add("banned_topic", f"{category} content: {matched!r}", sentence, matched)
        if not has_name:
            for matched in insult_hits:
                add(
                    "banned_topic",
                    f"possible personal insult: {matched!r}",
                    sentence,
                    matched,
                )

    # (4) closed-world
    for token in _closed_world(working, narration):
        add(
            "hallucination",
            f"token not found in the facts payload: {token!r}",
            _containing_sentence(pairs, token),
            token,
        )

    # (5) slop / tone
    for sentence, reported in pairs:
        for pattern in lists.slop:
            match = pattern.search(sentence)
            if match:
                add("slop", f"slop phrase: {match.group(0)!r}", reported, match.group(0))

    return SafetyReport(findings=tuple(findings))
