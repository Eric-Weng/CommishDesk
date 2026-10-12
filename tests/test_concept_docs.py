"""Concept docs are the stat manifest (Story 8.11).

``docs/concepts/<doc_id>.md`` explains every stat group of the Facts JSON, and
``docs/facts-schema.json`` is the manifest those docs are checked against, in
both directions:

* every *leaf* property path of ``WeeklyFacts`` and ``DraftRecapFacts`` is
  covered by exactly one claim (a ``facts_keys`` entry of a ``stat`` doc, or an
  entry of ``docs/concepts/not-a-stat.toml``);
* every claim resolves to a real path.

**Key universe.** Paths are root-qualified (``weekly.teams[].season.luck``,
``draft_recap.picks[].delta``), arrays are written ``[]``, ``$ref`` and nullable
``anyOf`` are followed. A claim covers its own path and every descendant, so
two claims may not overlap (equal or nested). A *container* path (one that has
child properties, such as ``weekly.teams[].season``) is valid as a claim but
needs no claim of its own: it is explained by whichever claims cover its
leaves. Without that rule a container could never be split between two docs.

**Doc shape.** Frontmatter is a restricted YAML subset parsed here with the
standard library: ``key: value`` scalars and a ``facts_keys:`` list of ``- ``
lines. A ``stat`` body has the four ``##`` headings Definition, Worked example,
Code, Edge cases, in that order, each non-empty. The worked example holds one
fenced ``json`` block that must be a subset of the named fixture, and the code
section holds a ``Source:`` line and one fenced ``python`` block that must be
the source function from ``source``.

The checks are plain functions returning error strings so the failure rows of
the spec can be exercised on synthetic input.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from commishdesk.facts.schema import SCHEMA_VERSION
from tests.conftest import REPO_ROOT

CONCEPTS_DIR = REPO_ROOT / "docs" / "concepts"
SCHEMA_PATH = REPO_ROOT / "docs" / "facts-schema.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

ROOTS: Mapping[str, str] = {"weekly": "WeeklyFacts", "draft_recap": "DraftRecapFacts"}
KINDS = ("stat", "guide")
STAT_HEADINGS = ("Definition", "Worked example", "Code", "Edge cases")
COMMON_KEYS = ("doc_id", "kind", "title", "schema_min")
STAT_KEYS = (*COMMON_KEYS, "facts_keys", "fixture", "source")
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
FENCE = re.compile(r"^```(\w*)\s*$")
# Plain-prose guard: no competing newsletter or stats product, nothing gated or cut off.
BANNED_NAMES = (
    "league legacy",
    "fantasypros",
    "fantasy footballers",
    "footballguys",
    "rotowire",
    "numberfire",
)
BANNED_MARKERS = re.compile(r"premium[- ]only|\(premium\)|premium chip|\[truncated\]|\.\.\. ?truncated", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Key universe
# --------------------------------------------------------------------------- #


MAX_DEPTH = 40
"""Nesting guard: the committed models are shallow, so a deeper walk means a recursive model."""


def _resolve(node: Mapping[str, Any], defs: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Follow ``$ref`` and ``anyOf``/``allOf`` to the concrete (non-null) nodes."""
    if "$ref" in node:
        return _resolve(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    branches = [*node.get("anyOf", []), *node.get("allOf", [])]
    if branches:
        return [concrete for branch in branches for concrete in _resolve(branch, defs)]
    return [node]


def _walk(node: Mapping[str, Any], prefix: str, defs: Mapping[str, Any], depth: int, out: list[str]) -> None:
    if depth > MAX_DEPTH:
        raise ValueError(f"{prefix}: schema nests deeper than {MAX_DEPTH} levels (recursive model?)")
    for concrete in _resolve(node, defs):
        if concrete.get("type") == "array":
            items = concrete.get("items")  # a fixed-length tuple has no ``items``: a leaf
            if items:
                _walk(items, f"{prefix}[]", defs, depth + 1, out)
        for name, child in concrete.get("properties", {}).items():
            path = f"{prefix}.{name}"
            out.append(path)
            _walk(child, path, defs, depth + 1, out)


def schema_paths(schema: Mapping[str, Any]) -> list[str]:
    """Every property path of both roots, root-qualified, in schema order."""
    defs = schema["$defs"]
    out: list[str] = []
    for root, model in ROOTS.items():
        _walk({"$ref": f"#/$defs/{model}"}, root, defs, 0, out)
    return out


def leaf_paths(paths: Iterable[str]) -> list[str]:
    """Paths with no child property. Only these need a covering claim."""
    ordered = list(paths)
    return [p for p in ordered if not any(covers(p, q) and p != q for q in ordered)]


def covers(claim: str, path: str) -> bool:
    """A claim covers its own path and every descendant."""
    return path == claim or path.startswith((f"{claim}.", f"{claim}["))


# --------------------------------------------------------------------------- #
# Frontmatter and body
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Doc:
    file: str
    meta: dict[str, Any]
    body: str
    errors: tuple[str, ...] = ()

    @property
    def doc_id(self) -> str:
        return str(self.meta.get("doc_id", ""))

    @property
    def kind(self) -> str:
        return str(self.meta.get("kind", ""))

    @property
    def keys(self) -> list[str]:
        value = self.meta.get("facts_keys", [])
        return list(value) if isinstance(value, list) else []


def parse_doc(file: str, text: str) -> Doc:
    """Split a doc into frontmatter and body using the restricted YAML subset."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return Doc(file, {}, text, (f"{file}: missing frontmatter opening '---'",))
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return Doc(file, {}, text, (f"{file}: frontmatter is never closed",))

    meta: dict[str, Any] = {}
    errors: list[str] = []
    current_list: str | None = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        item = re.match(r"^\s*-\s+(.*\S)\s*$", raw)
        if item and current_list is not None:
            meta[current_list].append(_scalar(item.group(1)))
            continue
        pair = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*\S))?\s*$", raw)
        if not pair:
            errors.append(f"{file}: unsupported frontmatter line {raw!r}")
            current_list = None
            continue
        key, value = pair.group(1), pair.group(2)
        if key in meta:
            errors.append(f"{file}: duplicate frontmatter key {key!r}")
        if value is None:
            meta[key] = []
            current_list = key
        else:
            meta[key] = _scalar(value)
            current_list = None
    return Doc(file, meta, "\n".join(lines[end + 1 :]), tuple(errors))


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def sections(body: str) -> list[tuple[str, list[str]]]:
    """``##`` sections of a body as ``(heading, lines)``; fenced lines never start one."""
    out: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in body.split("\n"):
        if FENCE.match(line) or line.strip() == "```":
            in_fence = not in_fence
        heading = re.match(r"^##\s+(.*\S)\s*$", line)
        if heading and not in_fence:
            out.append((heading.group(1), []))
        elif out:
            out[-1][1].append(line)
    return out


def fenced_blocks(lines: list[str]) -> list[tuple[str, str]]:
    """``(language, text)`` of each fenced block in *lines*."""
    blocks: list[tuple[str, str]] = []
    lang: str | None = None
    buf: list[str] = []
    for line in lines:
        match = FENCE.match(line)
        if lang is None and match:
            lang, buf = match.group(1), []
        elif lang is not None and line.strip() == "```":
            blocks.append((lang, "\n".join(buf)))
            lang = None
        elif lang is not None:
            buf.append(line)
    return blocks


def prose(lines: list[str]) -> str:
    """The lines of a section outside any fenced block."""
    out: list[str] = []
    in_fence = False
    for line in lines:
        if FENCE.match(line) or line.strip() == "```":
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# Checks (each returns a list of error strings)
# --------------------------------------------------------------------------- #


def check_shape(doc: Doc, expected_id: str) -> list[str]:
    """Frontmatter fields, id rules and the body template."""
    errors = list(doc.errors)
    meta = doc.meta
    if errors:
        return errors
    kind = doc.kind
    if kind not in KINDS:
        return [f"{doc.file}: kind must be one of {KINDS}, got {kind!r}"]
    required = STAT_KEYS if kind == "stat" else COMMON_KEYS
    for key in required:
        if not meta.get(key):
            errors.append(f"{doc.file}: missing frontmatter field {key!r}")
    for key in meta:
        if key not in required:
            errors.append(f"{doc.file}: unexpected frontmatter field {key!r} for kind {kind!r}")
    if errors:
        return errors

    if not KEBAB.match(doc.doc_id):
        errors.append(f"{doc.file}: doc_id {doc.doc_id!r} is not lowercase kebab-case")
    if doc.doc_id != expected_id:
        errors.append(f"{doc.file}: doc_id {doc.doc_id!r} does not equal the filename {expected_id!r}")
    schema_min = str(meta["schema_min"])
    if not SEMVER.match(schema_min):
        errors.append(f"{doc.file}: schema_min {schema_min!r} is not a semantic version")
    elif _version(schema_min) > _version(SCHEMA_VERSION):
        errors.append(f"{doc.file}: schema_min {schema_min} is newer than the schema {SCHEMA_VERSION}")

    found = sections(doc.body)
    if kind == "guide":
        if not doc.body.strip():
            errors.append(f"{doc.file}: guide body is empty")
        return errors + _check_prose(doc)

    if not isinstance(meta["facts_keys"], list) or not meta["facts_keys"]:
        errors.append(f"{doc.file}: facts_keys must be a non-empty list")
    headings = tuple(name for name, _ in found)
    if headings != STAT_HEADINGS:
        errors.append(f"{doc.file}: stat headings must be exactly {list(STAT_HEADINGS)} in order, got {list(headings)}")
        return errors + _check_prose(doc)
    for name, lines in found:
        if not "\n".join(lines).strip():
            errors.append(f"{doc.file}: section {name!r} is empty")
    return errors + _check_prose(doc)


def _check_prose(doc: Doc) -> list[str]:
    errors: list[str] = []
    text = f"{doc.meta.get('title', '')}\n{doc.body}".lower()
    for name in BANNED_NAMES:
        if name in text:
            errors.append(f"{doc.file}: names a competing product ({name!r})")
    marker = BANNED_MARKERS.search(f"{doc.meta.get('title', '')}\n{doc.body}")
    if marker:
        errors.append(f"{doc.file}: gated or truncated marker {marker.group(0)!r}")
    return errors


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def check_coverage(
    paths: Iterable[str], docs: Iterable[Doc], not_a_stat: Iterable[str]
) -> list[str]:
    """Both directions: every leaf covered once, every claim real, no overlap."""
    path_list = list(paths)
    known = set(path_list)
    claims: list[tuple[str, str]] = [(doc.file, key) for doc in docs if doc.kind == "stat" for key in doc.keys]
    claims += [("not-a-stat.toml", key) for key in not_a_stat]

    errors: list[str] = []
    for owner, claim in claims:
        if claim not in known:
            errors.append(f"{owner}: claim {claim!r} is not a path in docs/facts-schema.json")
    for i, (owner_a, a) in enumerate(claims):
        for owner_b, b in claims[i + 1 :]:
            if covers(a, b) or covers(b, a):
                errors.append(f"overlapping claims {a!r} ({owner_a}) and {b!r} ({owner_b})")
    for leaf in leaf_paths(path_list):
        owners = [owner for owner, claim in claims if covers(claim, leaf)]
        if not owners:
            errors.append(f"{leaf}: no doc or not-a-stat entry covers this path")
        elif len(owners) > 1:
            errors.append(f"{leaf}: covered {len(owners)} times ({', '.join(owners)})")
    return errors


def json_subset(sub: Any, whole: Any) -> bool:
    """*sub* is contained in *whole*, values equal.

    Objects: every key of *sub* is in *whole* and its value is a subset. Lists:
    each element of *sub* is a subset of some later-or-equal element of *whole*,
    in order, which lets an example pick one team out of ``teams[]``. Scalars
    are equal."""
    if isinstance(sub, dict):
        return isinstance(whole, dict) and all(k in whole and json_subset(v, whole[k]) for k, v in sub.items())
    if isinstance(sub, list):
        if not isinstance(whole, list) or (not sub and whole):
            return False
        cursor = 0
        for element in sub:
            while cursor < len(whole) and not json_subset(element, whole[cursor]):
                cursor += 1
            if cursor == len(whole):
                return False
            cursor += 1
        return True
    return type(sub) is type(whole) and sub == whole


def check_example(doc: Doc, fixtures_dir: Path = FIXTURES_DIR) -> list[str]:
    """The worked example is a fenced json block that is a subset of the fixture."""
    section = dict(sections(doc.body)).get("Worked example")
    if section is None:
        return [f"{doc.file}: no Worked example section"]
    fixture = fixtures_dir / str(doc.meta.get("fixture", ""))
    if not fixture.is_file():
        return [f"{doc.file}: fixture {doc.meta.get('fixture')!r} does not exist under tests/fixtures"]
    blocks = [text for lang, text in fenced_blocks(section) if lang == "json"]
    if len(blocks) != 1:
        return [f"{doc.file}: Worked example needs exactly one json block, found {len(blocks)}"]
    if not prose(section):
        return [f"{doc.file}: Worked example has no prose walking the numbers"]
    try:
        example = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        return [f"{doc.file}: worked example json does not parse ({exc})"]
    if not isinstance(example, dict):
        return [f"{doc.file}: worked example json must be an object"]
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    if not json_subset(example, expected):
        return [f"{doc.file}: worked example json is not a subset of {doc.meta['fixture']} (stale example)"]
    return []


def source_of(source: str, repo_root: Path = REPO_ROOT) -> str:
    """The text of the ``path/to/module.py::function`` definition."""
    path_part, _, name = source.partition("::")
    path = repo_root / path_part
    if not name or not path.is_file():
        raise LookupError(f"source {source!r} does not name an existing module and function")
    text = path.read_text(encoding="utf-8")
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            lines = text.split("\n")[node.lineno - 1 : node.end_lineno]
            return "\n".join(lines)
    raise LookupError(f"{path_part} has no top-level function {name!r}")


def normalise(text: str) -> str:
    return " ".join(text.split())


def check_code(doc: Doc, repo_root: Path = REPO_ROOT) -> list[str]:
    """The Code section is a Source: line plus the verbatim source function."""
    section = dict(sections(doc.body)).get("Code")
    if section is None:
        return [f"{doc.file}: no Code section"]
    source = str(doc.meta.get("source", ""))
    first = next((line for line in section if line.strip()), "")
    if not first.startswith("Source:") or source not in first:
        return [f"{doc.file}: Code must open with a 'Source:' line naming {source!r}"]
    blocks = [text for lang, text in fenced_blocks(section) if lang == "python"]
    if len(blocks) != 1:
        return [f"{doc.file}: Code needs exactly one python block, found {len(blocks)}"]
    try:
        expected = source_of(source, repo_root)
    except LookupError as exc:
        return [f"{doc.file}: {exc}"]
    if normalise(blocks[0]) != normalise(expected):
        return [f"{doc.file}: code excerpt differs from {source} (stale excerpt)"]
    return []


def check_not_a_stat(data: Mapping[str, Any]) -> list[str]:
    """``not-a-stat.toml`` is a list of ``[[exclude]]`` tables with path and reason."""
    entries = data.get("exclude")
    if not isinstance(entries, list) or not entries:
        return ["not-a-stat.toml: needs [[exclude]] entries"]
    errors: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"path", "reason"}:
            errors.append(f"not-a-stat.toml: entry {index} must have exactly 'path' and 'reason'")
            continue
        if not str(entry["path"]).strip() or not str(entry["reason"]).strip():
            errors.append(f"not-a-stat.toml: entry {index} has an empty path or reason")
    extras = set(data) - {"exclude"}
    if extras:
        errors.append(f"not-a-stat.toml: unexpected top-level keys {sorted(extras)}")
    return errors


def check_retired(data: Mapping[str, Any], live_ids: set[str]) -> list[str]:
    """``retired.toml`` is a ``[retired]`` table of ``old = "new"`` doc ids."""
    table = data.get("retired")
    if not isinstance(table, dict) or set(data) != {"retired"}:
        return ["retired.toml: must hold only a [retired] table"]
    errors: list[str] = []
    for old, new in table.items():
        if not isinstance(new, str) or not KEBAB.match(old) or not KEBAB.match(new):
            errors.append(f"retired.toml: {old!r} = {new!r} must map one kebab-case doc_id to another")
        elif old in live_ids:
            errors.append(f"retired.toml: {old!r} is still a live doc_id")
        elif new not in live_ids:
            errors.append(f"retired.toml: {old!r} points at {new!r}, which is not a doc_id")
    return errors


# --------------------------------------------------------------------------- #
# Loading the committed set
# --------------------------------------------------------------------------- #


def load_docs(directory: Path = CONCEPTS_DIR) -> list[Doc]:
    return [parse_doc(path.name, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.md"))]


def load_toml(name: str) -> dict[str, Any]:
    return tomllib.loads((CONCEPTS_DIR / name).read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _excluded() -> list[str]:
    return [str(entry["path"]) for entry in load_toml("not-a-stat.toml").get("exclude", [])]


def _stat_doc(**overrides: Any) -> Doc:
    """A copy of a committed stat doc with its frontmatter or body replaced."""
    base = next(doc for doc in load_docs() if doc.doc_id == "all-play")
    meta = {**base.meta, **{k: v for k, v in overrides.items() if k != "body"}}
    return Doc(base.file, meta, overrides.get("body", base.body))


# --------------------------------------------------------------------------- #
# The committed docs
# --------------------------------------------------------------------------- #


def test_concepts_directory_holds_only_known_files() -> None:
    names = {p.name for p in CONCEPTS_DIR.iterdir()}
    assert {"not-a-stat.toml", "retired.toml"} <= names
    stray = sorted(n for n in names if not n.endswith(".md") and n not in {"not-a-stat.toml", "retired.toml"})
    assert not stray, f"unexpected files in docs/concepts: {stray}"
    assert any(doc.kind == "guide" for doc in load_docs()), "the guide is missing"


def test_every_doc_is_well_formed() -> None:
    docs = load_docs()
    assert docs
    errors: list[str] = []
    for doc in docs:
        errors += check_shape(doc, Path(doc.file).stem)
    ids = [doc.doc_id for doc in docs]
    errors += [f"duplicate doc_id {i!r}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    assert not errors, "\n".join(errors)


def test_schema_is_covered_both_ways() -> None:
    errors = check_coverage(schema_paths(_schema()), load_docs(), _excluded())
    assert not errors, "\n".join(errors)


def test_worked_examples_match_their_fixtures() -> None:
    errors: list[str] = []
    for doc in load_docs():
        if doc.kind == "stat":
            errors += check_example(doc)
    assert not errors, "\n".join(errors)


def test_code_excerpts_are_the_real_functions() -> None:
    errors: list[str] = []
    for doc in load_docs():
        if doc.kind == "stat":
            errors += check_code(doc)
    assert not errors, "\n".join(errors)


def test_exclusion_files_have_the_right_shape() -> None:
    assert not check_not_a_stat(load_toml("not-a-stat.toml"))
    live = {doc.doc_id for doc in load_docs()}
    assert not check_retired(load_toml("retired.toml"), live)


def test_the_docs_do_not_ship_in_the_wheel() -> None:
    assert not (REPO_ROOT / "commishdesk" / "docs").exists()
    assert not list((REPO_ROOT / "commishdesk").rglob("concepts")), "concept docs stay outside the package"


# --------------------------------------------------------------------------- #
# The failure rows of the spec, on synthetic input
# --------------------------------------------------------------------------- #


def test_schema_walker_follows_refs_nullables_and_arrays() -> None:
    schema = {
        "$defs": {
            "WeeklyFacts": {
                "type": "object",
                "properties": {
                    "plain": {"type": "integer"},
                    "rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}},
                    "maybe": {"anyOf": [{"$ref": "#/$defs/Row"}, {"type": "null"}]},
                    "pair": {"type": "array", "items": {"type": "array", "minItems": 2, "prefixItems": []}},
                },
            },
            "DraftRecapFacts": {"type": "object", "properties": {"x": {"type": "string"}}},
            "Row": {"type": "object", "properties": {"a": {"type": "number"}}},
        }
    }
    assert schema_paths(schema) == [
        "weekly.plain",
        "weekly.rows",
        "weekly.rows[].a",
        "weekly.maybe",
        "weekly.maybe.a",
        "weekly.pair",
        "draft_recap.x",
    ]
    assert leaf_paths(schema_paths(schema)) == [
        "weekly.plain",
        "weekly.rows[].a",
        "weekly.maybe.a",
        "weekly.pair",
        "draft_recap.x",
    ]


def test_committed_schema_has_the_expected_roots() -> None:
    paths = schema_paths(_schema())
    assert "weekly.teams[].season.luck" in paths
    assert "draft_recap.picks[].delta" in paths
    assert {p.split(".")[0] for p in paths} == set(ROOTS)


def test_a_new_schema_field_outside_any_claim_fails_naming_the_path() -> None:
    schema = copy.deepcopy(_schema())
    schema["$defs"]["WeeklyFacts"]["properties"]["brand_new_stat"] = {"type": "number"}
    errors = check_coverage(schema_paths(schema), load_docs(), _excluded())
    assert any("weekly.brand_new_stat" in e for e in errors), errors


def test_a_new_field_under_a_claimed_group_is_covered_by_the_prefix() -> None:
    schema = copy.deepcopy(_schema())
    schema["$defs"]["WeeklyAllPlay"]["properties"]["fresh"] = {"type": "integer"}
    assert not check_coverage(schema_paths(schema), load_docs(), _excluded())


def test_duplicate_and_nested_claims_fail_naming_both_docs() -> None:
    docs = load_docs()
    other = next(d for d in docs if d.doc_id == "expected-wins-and-luck")
    dup = Doc("dup.md", {**other.meta, "facts_keys": ["weekly.teams[].season.all_play"]}, other.body)
    errors = check_coverage(schema_paths(_schema()), [*docs, dup], _excluded())
    assert any("all-play.md" in e and "dup.md" in e for e in errors), errors

    nested = Doc("nested.md", {**other.meta, "facts_keys": ["weekly.teams[].season.all_play.pct"]}, other.body)
    errors = check_coverage(schema_paths(_schema()), [*docs, nested], _excluded())
    assert any("all-play.md" in e and "nested.md" in e for e in errors), errors


def test_claim_overlapping_not_a_stat_fails() -> None:
    errors = check_coverage(schema_paths(_schema()), load_docs(), [*_excluded(), "weekly.teams[].season.luck"])
    assert any("expected-wins-and-luck.md" in e and "not-a-stat.toml" in e for e in errors), errors


def test_a_dead_claim_fails_naming_the_doc() -> None:
    docs = load_docs()
    target = next(d for d in docs if d.doc_id == "streaks")
    dead = Doc(target.file, {**target.meta, "facts_keys": [*target.keys, "weekly.teams[].season.nope"]}, target.body)
    errors = check_coverage(schema_paths(_schema()), [dead if d is target else d for d in docs], _excluded())
    assert any("streaks.md" in e and "weekly.teams[].season.nope" in e for e in errors), errors


def test_a_dead_not_a_stat_entry_fails() -> None:
    errors = check_coverage(schema_paths(_schema()), load_docs(), [*_excluded(), "weekly.ghost"])
    assert any("not-a-stat.toml" in e and "weekly.ghost" in e for e in errors), errors


def test_a_stale_example_fails_naming_the_doc() -> None:
    doc = _stat_doc()
    section = dict(sections(doc.body))["Worked example"]
    block = next(t for lang, t in fenced_blocks(section) if lang == "json")
    mutated = re.sub(r"(\d+)", lambda m: str(int(m.group(1)) + 1), block, count=1)
    assert mutated != block
    stale = _stat_doc(body=doc.body.replace(block, mutated))
    errors = check_example(stale)
    assert errors and "all-play.md" in errors[0] and "stale" in errors[0]


def test_a_stale_excerpt_fails_naming_the_doc() -> None:
    doc = _stat_doc()
    block = next(t for lang, t in fenced_blocks(dict(sections(doc.body))["Code"]) if lang == "python")
    stale = _stat_doc(body=doc.body.replace(block, block.replace("wins", "victories", 1)))
    errors = check_code(stale)
    assert errors and "all-play.md" in errors[0] and "stale" in errors[0]


def test_a_missing_source_or_fixture_fails() -> None:
    doc = _stat_doc()
    renamed = _stat_doc(
        source="commishdesk/stats/weekly.py::no_such_function",
        body=doc.body.replace("_all_play_totals", "no_such_function"),
    )
    missing_function = check_code(renamed)
    assert missing_function and "no top-level function" in missing_function[0]
    missing_fixture = check_example(_stat_doc(fixture="facts/not-a-file.json"))
    assert missing_fixture and "does not exist" in missing_fixture[0]


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"doc_id": "All_Play"}, "kebab-case"),
        ({"doc_id": "other-name"}, "filename"),
        ({"kind": "essay"}, "kind must be"),
        ({"schema_min": "next"}, "semantic version"),
        ({"schema_min": "99.0.0"}, "newer than the schema"),
        ({"title": ""}, "missing frontmatter field 'title'"),
        ({"fixture": ""}, "missing frontmatter field 'fixture'"),
        ({"facts_keys": []}, "missing frontmatter field 'facts_keys'"),
        ({"surprise": "x"}, "unexpected frontmatter field"),
    ],
)
def test_bad_frontmatter_fails_naming_the_file(override: dict[str, Any], fragment: str) -> None:
    errors = check_shape(_stat_doc(**override), "all-play")
    assert any("all-play.md" in e and fragment in e for e in errors), errors


def test_missing_or_misordered_heading_or_empty_section_fails() -> None:
    doc = _stat_doc()
    no_edge = doc.body.split("## Edge cases")[0]
    assert any("headings" in e for e in check_shape(_stat_doc(body=no_edge), "all-play"))
    swapped = (
        doc.body.replace("## Definition", "## TMP").replace("## Code", "## Definition").replace("## TMP", "## Code")
    )
    assert any("headings" in e for e in check_shape(_stat_doc(body=swapped), "all-play"))
    emptied = re.sub(r"(## Edge cases\n).*", r"\1", doc.body, flags=re.DOTALL)
    assert any("'Edge cases' is empty" in e for e in check_shape(_stat_doc(body=emptied), "all-play"))


def test_a_competitor_name_or_gated_marker_fails() -> None:
    doc = _stat_doc()
    named = check_shape(_stat_doc(body=doc.body + "\nLike League Legacy.\n"), "all-play")
    assert any("competing product" in e for e in named)
    gated = check_shape(_stat_doc(body=doc.body + "\n[truncated]\n"), "all-play")
    assert any("gated or truncated" in e for e in gated)


def test_duplicate_ids_and_unparseable_frontmatter_are_caught() -> None:
    assert parse_doc("x.md", "no frontmatter").errors
    assert parse_doc("x.md", "---\ndoc_id: x\n").errors
    assert parse_doc("x.md", "---\ndoc_id: x\ndoc_id: y\n---\nbody").errors
    assert parse_doc("x.md", "---\nnested:\n  key: value\n---\n").errors


def test_frontmatter_parser_reads_scalars_and_lists() -> None:
    doc = parse_doc("x.md", "---\ndoc_id: x\ntitle: 'A: b'\nfacts_keys:\n  - a.b\n  - c[].d\n---\nbody\n")
    assert not doc.errors
    assert doc.meta == {"doc_id": "x", "title": "A: b", "facts_keys": ["a.b", "c[].d"]}


def test_json_subset_rules() -> None:
    whole = {"a": 1, "rows": [{"id": "1", "v": 2.5}, {"id": "2", "v": 3}], "n": None}
    assert json_subset({"a": 1, "n": None}, whole)
    assert json_subset({"rows": [{"id": "2", "v": 3}]}, whole)
    assert json_subset({"rows": [{"id": "1"}, {"id": "2"}]}, whole)
    assert not json_subset({"rows": [{"id": "2"}, {"id": "1"}]}, whole)
    assert not json_subset({"a": 2}, whole)
    assert not json_subset({"a": True}, whole)
    assert not json_subset({"missing": 1}, whole)


def test_exclusion_file_shape_rules() -> None:
    assert check_not_a_stat({})
    assert check_not_a_stat({"exclude": [{"path": "weekly.week"}]})
    assert check_not_a_stat({"exclude": [{"path": "weekly.week", "reason": " "}]})
    assert not check_not_a_stat({"exclude": [{"path": "weekly.week", "reason": "the week number"}]})
    assert check_retired({"retired": {"old-id": "all-play"}}, {"all-play"}) == []
    assert check_retired({"retired": {"old-id": "missing"}}, {"all-play"})
    assert check_retired({"retired": {"all-play": "all-play"}}, {"all-play"})
    assert check_retired({"other": {}}, {"all-play"})
