"""Story 1.6: the four extension zones, their protocols, docs, and the one-reference-impl guard.

One test per I/O & Edge-Case Matrix row, plus the ``_count_impls`` helper's own unit test.
Story 1.6 shipped zero reference implementations; Story 2.2 (Epic 2) landed the first —
`adapters/sleeper.py` — so `adapters` now counts 1 and the other three zones stay at 0.
"""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_type_hints

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "commishdesk"
DOCS = REPO_ROOT / "docs"

# zone dir -> (protocol name, module-level symbol)
ZONES: dict[str, str] = {
    "adapters": "Adapter",
    "voices": "Voice",
    "themes": "Renderer",
    "statmods": "StatModule",
}
EVAL_PATHS = [f"tests/eval/{zone}/" for zone in ZONES]


# --------------------------------------------------------------------------- #
# The counting helper (Design Notes: _count_impls)
# --------------------------------------------------------------------------- #


def _count_impls(pkg_dir: Path) -> int:
    """Number of reference implementations directly under a zone package: each ``*.py``
    file that is neither ``__init__.py`` nor underscore-prefixed, **plus** each
    subdirectory whose name is not underscore-prefixed and not ``__pycache__`` that
    *either* contains an ``__init__.py`` (a subpackage) *or* contains any
    non-underscore-prefixed ``*.py`` module at any depth (a namespace-package impl).
    A directory meeting both conditions still counts once.

    retro A2: the subpackage arm required ``(p / "__init__.py").is_file()``, but a PEP 420
    namespace package is importable with no ``__init__.py`` anywhere -- so two full
    adapters could sit in one zone and clear the at-most-one ceiling. The namespace-package
    arm closes that."""
    modules = sum(
        1
        for p in pkg_dir.glob("*.py")
        if p.name != "__init__.py" and not p.name.startswith("_")
    )
    subpackages = 0
    for p in pkg_dir.iterdir():
        if not p.is_dir() or p.name.startswith("_") or p.name == "__pycache__":
            continue
        has_init = (p / "__init__.py").is_file()
        has_module = any(not m.name.startswith("_") for m in p.rglob("*.py"))
        if has_init or has_module:
            subpackages += 1
    return modules + subpackages


def test_count_impls_helper_tripwire(tmp_path: Path) -> None:
    """Row: Count helper — tripwire. Two non-underscore modules plus a non-underscore
    subpackage => 3; anything > 1 must fail the guard's assertion."""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "_private.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "_helpers").mkdir()
    (tmp_path / "_helpers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "notapkg").mkdir()  # empty dir — no __init__.py, no module — not an impl
    assert _count_impls(tmp_path) == 0

    (tmp_path / "one.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("x = 2\n", encoding="utf-8")
    assert _count_impls(tmp_path) == 2

    (tmp_path / "sleeper").mkdir()
    (tmp_path / "sleeper" / "__init__.py").write_text("x = 3\n", encoding="utf-8")
    assert _count_impls(tmp_path) == 3  # the subpackage counts
    assert not (_count_impls(tmp_path) <= 1)  # the same assertion the guard makes

    # retro A2: a PEP 420 namespace package — a subdir holding a module with no
    # __init__.py anywhere — is importable and must count as one impl.
    (tmp_path / "nspkg").mkdir()
    (tmp_path / "nspkg" / "impl.py").write_text("x = 4\n", encoding="utf-8")
    assert _count_impls(tmp_path) == 4  # the namespace package counts too

    # a subdir with BOTH an __init__.py and a module still counts once, not twice
    (tmp_path / "nspkg" / "__init__.py").write_text("", encoding="utf-8")
    assert _count_impls(tmp_path) == 4


# --------------------------------------------------------------------------- #
# Row: Zone import
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("zone,protocol", ZONES.items(), ids=list(ZONES))
def test_zone_package_imports_and_exposes_a_runtime_checkable_protocol(
    zone: str, protocol: str
) -> None:
    module = importlib.import_module(f"commishdesk.{zone}")
    assert module.__doc__, f"commishdesk.{zone} is missing its module docstring"
    assert hasattr(module, protocol), f"commishdesk.{zone}.{protocol} is missing"
    proto = getattr(module, protocol)
    # @runtime_checkable protocols accept isinstance() checks; a plain Protocol raises.
    assert getattr(proto, "_is_runtime_protocol", False), (
        f"{protocol} is not @runtime_checkable"
    )
    isinstance(object(), proto)  # must not raise


def test_v0_marker_present_in_each_zone_module() -> None:
    for zone in ZONES:
        src = (PKG_ROOT / zone / "__init__.py").read_text(encoding="utf-8")
        assert "# v0" in src, f"commishdesk/{zone}/__init__.py is missing the # v0 marker"


def test_facts_alias_exists_and_renderer_statmodule_reference_it() -> None:
    facts = importlib.import_module("commishdesk.facts")
    assert hasattr(facts, "FactsJSON")
    for zone in ("themes", "statmods"):
        src = (PKG_ROOT / zone / "__init__.py").read_text(encoding="utf-8")
        assert "from commishdesk.facts import FactsJSON" in src
    # Adapter and Voice must NOT import facts.
    for zone in ("adapters", "voices"):
        src = (PKG_ROOT / zone / "__init__.py").read_text(encoding="utf-8")
        assert "commishdesk.facts" not in src, f"{zone} must not import facts"


def _live_protocols() -> dict[str, type]:
    from commishdesk.adapters import Adapter
    from commishdesk.statmods import StatModule
    from commishdesk.themes import Renderer
    from commishdesk.voices import Voice

    return {
        "Adapter": Adapter,
        "Voice": Voice,
        "Renderer": Renderer,
        "StatModule": StatModule,
    }


def _protocol_members(cls: type) -> list[str]:
    return sorted(getattr(cls, "__protocol_attrs__", ()))


def test_protocol_member_surfaces_match_the_v0_spec() -> None:
    protos = _live_protocols()
    assert _protocol_members(protos["Adapter"]) == ["fetch"]
    assert _protocol_members(protos["Renderer"]) == ["render"]
    assert _protocol_members(protos["Voice"]) == ["banned_topics", "system_prompt"]
    assert _protocol_members(protos["StatModule"]) == ["compute", "module_id"]
    # "one or two members, no more" (frozen Always)
    for name, cls in protos.items():
        assert 1 <= len(_protocol_members(cls)) <= 2, name


def test_protocol_annotations_match_the_spec() -> None:
    protos = _live_protocols()

    assert get_type_hints(protos["Adapter"].fetch) == {
        "league_id": str,
        "return": Mapping[str, Any],
    }
    assert get_type_hints(protos["Renderer"].render)["return"] is str
    assert get_type_hints(protos["StatModule"].compute)["return"] == Mapping[str, object]

    voice_hints = get_type_hints(protos["Voice"])
    assert voice_hints["system_prompt"] is str
    assert voice_hints["banned_topics"] == frozenset[str]
    assert get_type_hints(protos["StatModule"])["module_id"] is str


def test_extending_doc_lists_every_live_protocol_member() -> None:
    """Row: Docs in sync — member names come from the live ``Protocol`` classes, not a
    hard-coded list, so a member added later without a doc update fails here."""
    text = (DOCS / "EXTENDING.md").read_text(encoding="utf-8")
    for name, cls in _live_protocols().items():
        assert name in text, f"docs/EXTENDING.md never mentions {name}"
        for member in _protocol_members(cls):
            assert member in text, f"docs/EXTENDING.md omits {name}.{member}"


# --------------------------------------------------------------------------- #
# Row: Count guard — empty  (the actual one-reference-impl rule)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("zone", list(ZONES), ids=list(ZONES))
def test_zone_holds_at_most_one_reference_implementation(zone: str) -> None:
    count = _count_impls(PKG_ROOT / zone)
    assert count <= 1, f"commishdesk/{zone}/ holds {count} reference impls (max 1)"


def test_every_zone_ships_zero_reference_impls_in_this_story() -> None:
    assert {zone: _count_impls(PKG_ROOT / zone) for zone in ZONES} == {
        "adapters": 1,
        "voices": 0,
        "themes": 0,
        "statmods": 0,
    }


# --------------------------------------------------------------------------- #
# Row: Stat-zone name
# --------------------------------------------------------------------------- #


def test_community_stat_zone_is_statmods_and_does_not_shadow_stats() -> None:
    assert (PKG_ROOT / "statmods" / "__init__.py").is_file()
    assert not (PKG_ROOT / "stats" / "statmods.py").exists()

    stats = importlib.import_module("commishdesk.stats")
    # commishdesk.stats is still the pipeline compute package, untouched.
    assert stats.__doc__ and stats.__doc__.startswith("Stage 2")
    assert Path(stats.__file__).parent.name == "stats"


# --------------------------------------------------------------------------- #
# Row: Zone READMEs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("zone,protocol", ZONES.items(), ids=list(ZONES))
def test_each_zone_has_a_readme_naming_its_protocol_and_the_rule(
    zone: str, protocol: str
) -> None:
    readme = PKG_ROOT / zone / "README.md"
    assert readme.is_file(), f"commishdesk/{zone}/README.md is missing"
    text = readme.read_text(encoding="utf-8")
    assert protocol in text
    assert "docs/EXTENDING.md" in text
    assert f"tests/eval/{zone}/" in text
    assert "test_extension_zones.py" in text


# --------------------------------------------------------------------------- #
# Row: Docs in sync
# --------------------------------------------------------------------------- #


def test_extending_doc_names_every_protocol_and_eval_path() -> None:
    text = (DOCS / "EXTENDING.md").read_text(encoding="utf-8")
    for protocol in ZONES.values():
        assert protocol in text, f"docs/EXTENDING.md never mentions {protocol}"
    for path in EVAL_PATHS:
        assert path in text, f"docs/EXTENDING.md never mentions {path}"
    # signatures
    for token in ("fetch(", "render(", "compute(", "system_prompt", "banned_topics"):
        assert token in text
    # explicit "prescribes no implementation"
    assert "prescribe" in text.lower()
    # links its companion doc
    assert "unclaimed-territory.md" in text
    # says where a non-reference implementation lives
    assert "fork" in text.lower()


# --------------------------------------------------------------------------- #
# Row: Eval dirs exist
# --------------------------------------------------------------------------- #


def test_all_eval_directories_exist_on_disk() -> None:
    assert (REPO_ROOT / "tests" / "eval" / "README.md").is_file()
    for zone in ZONES:
        d = REPO_ROOT / "tests" / "eval" / zone
        assert d.is_dir(), f"{d} is missing"
        assert (d / ".gitkeep").is_file(), f"{d}/.gitkeep is missing"


# --------------------------------------------------------------------------- #
# Row: Unclaimed territory
# --------------------------------------------------------------------------- #

_TICKET_PATTERNS = {
    "TODO(": re.compile(r"TODO\("),
    "issue ref (#123)": re.compile(r"#\d+"),
    "assignee (@handle)": re.compile(r"(?<![\w`])@\w+"),
    "ISO date": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "slash date": re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
}


def test_unclaimed_territory_is_invitations_not_tickets() -> None:
    doc = DOCS / "unclaimed-territory.md"
    text = doc.read_text(encoding="utf-8")
    assert text.strip(), "docs/unclaimed-territory.md is empty"
    assert len([ln for ln in text.splitlines() if ln.lstrip().startswith("- ")]) >= 4
    for label, pattern in _TICKET_PATTERNS.items():
        assert not pattern.search(text), f"unclaimed-territory.md reads like a ticket: {label}"


# --------------------------------------------------------------------------- #
# Row: README links resolve
# --------------------------------------------------------------------------- #


def test_readme_extending_section_links_resolve_on_disk() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Extending it" in readme
    # bound the slice to this section only — a link in a later section can't satisfy it.
    after = readme.split("## Extending it", 1)[1]
    section = re.split(r"\n## ", after, maxsplit=1)[0]
    for rel in ("docs/EXTENDING.md", "docs/unclaimed-territory.md"):
        assert rel in section, f"README 'Extending it' section never links {rel}"
        assert (REPO_ROOT / rel).is_file(), f"{rel} linked from README but missing on disk"


# --------------------------------------------------------------------------- #
# Row: Built wheel
# --------------------------------------------------------------------------- #


def _find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"
    return str(fallback) if fallback.exists() else None


def test_zone_packages_ship_in_the_wheel_and_import_from_it(tmp_path: Path) -> None:
    """Row: Built wheel. The four zone packages (with their protocol source) are in the
    wheel and import cleanly from an extracted copy on an isolated path; ``tests/`` and
    ``tools/`` stay out."""
    uv = _find_uv()
    if uv is None:
        pytest.skip("uv not available")
    build = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, wheels

    extract = tmp_path / "extract"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        zf.extractall(extract)

    for zone, protocol in ZONES.items():
        assert f"commishdesk/{zone}/__init__.py" in names, f"{zone} missing from wheel"
        assert protocol in (extract / "commishdesk" / zone / "__init__.py").read_text(
            encoding="utf-8"
        )
    offenders = [
        n for n in names if n.startswith(("tools/", "tests/")) or "/tools/" in n or "/tests/" in n
    ]
    assert not offenders, offenders

    # Import each zone from the extracted wheel copy on an isolated sys.path
    # (stdlib kept, repo tree and site-packages excluded).
    prog = (
        "import sys;"
        "root=sys.argv[1];"
        "sys.path=[root]+[p for p in sys.path if p and "
        "'site-packages' not in p.replace(chr(92),'/') and root not in p];"
        "import commishdesk.adapters, commishdesk.voices, commishdesk.themes,"
        " commishdesk.statmods, commishdesk.stats;"
        "from commishdesk.adapters import Adapter;"
        "from commishdesk.themes import Renderer;"
        "assert Adapter._is_runtime_protocol and Renderer._is_runtime_protocol;"
        "assert 'site-packages' not in (commishdesk.adapters.__file__ or '');"
        "print('ok')"
    )
    check = subprocess.run(
        [sys.executable, "-I", "-c", prog, str(extract)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "ok"
