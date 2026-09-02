"""Story 1.7: repo-hygiene checks for the ``.github/`` config files.

One test per I/O & Edge-Case Matrix row that concerns a workflow file or
``.github/dependabot.yml``. These are repo-inspection tests (they mirror
``test_extension_zones.py`` in style), not library-behavior tests, and they are not
shipped in the wheel.

The ``CLAUDE.md`` §4 rules (SHA-pinned ``uses:``, least-privilege ``permissions:``, no
``pull_request_target``, no secret exposed to a fork-triggered workflow, no untrusted
issue/PR/comment text) bind **every** workflow, so those checks fan out over every file
under ``.github/workflows/`` -- not just ``test.yml``. The exact ``permissions:`` shape
and the restricted trigger set are ``test.yml``-specific.

No new dependency: there is no stdlib YAML parser and PyYAML is forbidden, so every
assertion is against the raw file text via targeted regex / substring / line checks.
Where no ``.github/`` directory exists (e.g. an extracted sdist), every check
``pytest.skip``s -- there is nothing to inspect.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOW_DIR / "test.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

# Actions allowed by the spec (frozen "Ask First": anything else needs a renegotiation).
ALLOWED_ACTIONS = {"actions/checkout", "astral-sh/setup-uv"}

_USES_RE = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Untrusted-input interpolations that must never appear in a workflow (CLAUDE.md §4:
# "No step consumes issue / PR / comment text").
_UNTRUSTED_INTERP = (
    r"github\.event\.[A-Za-z0-9_.]*\bbody\b",
    r"github\.event\.[A-Za-z0-9_.]*\btitle\b",
    r"github\.event\.[A-Za-z0-9_.]*\bhead_ref\b",
    r"github\.head_ref\b",
)


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #


def _read_github_file(path: Path, github_root: Path = REPO_ROOT) -> str:
    """Return the file text, or ``pytest.skip`` when this tree has no ``.github/``
    (row: Out-of-tree -- a repo-hygiene check with nothing to check)."""
    if not (github_root / ".github").is_dir():
        pytest.skip("no .github/ in this tree -- repo-hygiene check, not library behavior")
    assert path.is_file(), f"{path} is missing"
    return path.read_text(encoding="utf-8")


def _workflow() -> str:
    return _read_github_file(WORKFLOW)


def _dependabot() -> str:
    return _read_github_file(DEPENDABOT)


def _all_workflows() -> list[tuple[str, str]]:
    """``(filename, text)`` for every file under ``.github/workflows/``; skips out-of-tree."""
    if not (REPO_ROOT / ".github").is_dir():
        pytest.skip("no .github/ in this tree -- repo-hygiene check, not library behavior")
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert files, "no workflow files under .github/workflows/"
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def _uses_refs(text: str) -> list[str]:
    return _USES_RE.findall(text)


def _uses_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if _USES_RE.match(ln)]


def _top_level_permissions(text: str) -> tuple[bool, str]:
    """``(present, body)`` for the top-level ``permissions:`` key. ``body`` is the inline
    value (``write-all``, ``{contents: read}``, …) or the space-joined indented entries."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^permissions:\s*(.*?)\s*(?:#.*)?$", ln)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline:
            return True, inline
        body: list[str] = []
        for sub in lines[i + 1:]:
            if not sub.strip() or sub.lstrip().startswith("#"):
                continue
            if len(sub) - len(sub.lstrip()) == 0:  # dedent to column 0 ends the block
                break
            body.append(sub.strip())
        return True, " ".join(body)
    return False, ""


def _on_block_top_keys(text: str) -> set[str]:
    """Top-level keys of the block-form ``on:`` mapping."""
    lines = text.splitlines()
    keys: set[str] = set()
    for i, ln in enumerate(lines):
        if not re.match(r"^on:\s*(?:#.*)?$", ln):
            continue
        for sub in lines[i + 1:]:
            if not sub.strip() or sub.lstrip().startswith("#"):
                continue
            indent = len(sub) - len(sub.lstrip())
            if indent == 0:
                return keys
            if indent == 2:
                km = re.match(r"^ {2}([A-Za-z_][\w-]*):", sub)
                if km:
                    keys.add(km.group(1))
        return keys
    raise AssertionError("no block-form on: mapping found")


# --------------------------------------------------------------------------- #
# Row: Out-of-tree
# --------------------------------------------------------------------------- #


def test_config_checks_skip_when_no_github_dir(tmp_path: Path) -> None:
    """Row: Out-of-tree. In a tree with no ``.github/`` these checks skip, not fail."""
    with pytest.raises(pytest.skip.Exception):
        _read_github_file(tmp_path / ".github" / "workflows" / "test.yml", github_root=tmp_path)


# --------------------------------------------------------------------------- #
# Workflow-wide safety (CLAUDE.md §4 -- binds EVERY workflow file)
# --------------------------------------------------------------------------- #


def test_every_workflow_pins_every_uses_to_a_full_commit_sha() -> None:
    """Row: SHA-pin guard, over every workflow. Every ``uses:`` value's text after ``@``
    is a 40-hex commit SHA -- a tag or branch ref fails here."""
    saw_a_ref = False
    for name, text in _all_workflows():
        for ref in _uses_refs(text):
            saw_a_ref = True
            assert "@" in ref, f"{name}: {ref!r} is not pinned at all"
            assert _SHA_RE.match(ref.split("@", 1)[1]), (
                f"{name}: {ref!r} is not pinned to a 40-hex commit SHA"
            )
    assert saw_a_ref, "no uses: entries found in any workflow"


def test_every_workflow_uses_only_allowlisted_actions() -> None:
    """Frozen 'Ask First': any Action beyond ``actions/checkout`` + ``astral-sh/setup-uv``
    needs a renegotiation -- checked over every workflow."""
    for name, text in _all_workflows():
        actions = {ref.split("@", 1)[0] for ref in _uses_refs(text)}
        assert actions <= ALLOWED_ACTIONS, f"{name}: unexpected Action(s) {actions - ALLOWED_ACTIONS}"


def test_every_uses_line_carries_a_version_comment() -> None:
    """Design Notes: each SHA pin carries a trailing ``# vX.Y.Z`` so Dependabot's
    github-actions updater bumps it with the comment intact. Selected via the anchored
    ``uses:`` regex so a ``run:``/``name:`` line that merely contains "uses:" is ignored."""
    for name, text in _all_workflows():
        for line in _uses_lines(text):
            assert re.search(r"#\s*v\d+\.\d+\.\d+", line), f"{name}: no version comment: {line!r}"


def test_every_workflow_has_a_least_privilege_top_level_permissions_block() -> None:
    """Row: Least privilege, over every workflow. Each declares a top-level
    ``permissions:`` block, and none grants ``write-all`` or any ``<scope>: write``."""
    for name, text in _all_workflows():
        present, body = _top_level_permissions(text)
        assert present, f"{name}: no top-level permissions: block"
        assert "write-all" not in body, f"{name}: permissions: write-all"
        for scope, mode in re.findall(r"([A-Za-z-]+):\s*(read|write|none)\b", body):
            assert mode != "write", f"{name}: permissions grants {scope}: write"


def test_no_workflow_uses_pull_request_target() -> None:
    """Row / frozen Never: ``pull_request_target`` appears in no workflow."""
    for name, text in _all_workflows():
        assert "pull_request_target" not in text, f"{name}: uses pull_request_target"


def test_no_workflow_references_a_repository_secret_other_than_github_token() -> None:
    """Frozen Never / CLAUDE.md §4: no secret exposed to a fork-triggered workflow. Only
    the auto-provisioned ``secrets.GITHUB_TOKEN`` is permitted anywhere."""
    for name, text in _all_workflows():
        leftover = re.sub(r"secrets\.GITHUB_TOKEN\b", "", text)
        assert "secrets." not in leftover, f"{name}: references a repository secret"


def test_no_workflow_interpolates_untrusted_input() -> None:
    """CLAUDE.md §4: no step consumes issue / PR / comment text. Reject any
    ``${{ github.event.*.body }}`` / ``.title`` / ``.head_ref`` / ``${{ github.head_ref }}``."""
    for name, text in _all_workflows():
        for pattern in _UNTRUSTED_INTERP:
            assert not re.search(pattern, text), f"{name}: untrusted interpolation {pattern!r}"


# --------------------------------------------------------------------------- #
# Row: Workflow shape (test.yml-specific)
# --------------------------------------------------------------------------- #


def test_test_workflow_triggers_are_exactly_push_pull_request_workflow_dispatch() -> None:
    """Row: Workflow shape. ``test.yml``'s ``on:`` block's top-level keys are exactly
    ``{push, pull_request, workflow_dispatch}`` -- no ``workflow_run`` / ``schedule`` /
    ``issue_comment`` / ``pull_request_target``. ``push`` is limited to ``main``."""
    text = _workflow()
    assert _on_block_top_keys(text) == {"push", "pull_request", "workflow_dispatch"}
    assert re.search(r"push:\s*\n\s*branches:\s*\[main\]", text), "push: is not limited to main"
    assert "pull_request_target" not in text


# --------------------------------------------------------------------------- #
# Row: Least privilege (test.yml-specific exact shape)
# --------------------------------------------------------------------------- #


def test_test_workflow_permissions_is_exactly_contents_read() -> None:
    """Row: Least privilege. ``test.yml`` has exactly one ``permissions:`` key in the
    whole file, at top level, whose only entry is ``contents: read``; no job re-declares
    it."""
    text = _workflow()
    assert text.count("permissions:") == 1, "more than one permissions: key in test.yml"
    present, body = _top_level_permissions(text)
    assert present, "permissions: is not a top-level block"
    # block form joins to "contents: read"; an inline {contents: read} normalizes the same
    assert re.sub(r"\s+", " ", body).strip("{} ") == "contents: read", body


# --------------------------------------------------------------------------- #
# Row: No secrets (test.yml-specific -- stricter: none at all)
# --------------------------------------------------------------------------- #


def test_test_workflow_references_no_secret_at_all() -> None:
    """Row: No secrets. The substring ``secrets.`` appears nowhere in ``test.yml`` -- not
    even ``secrets.GITHUB_TOKEN``; the offline suite needs no token."""
    assert "secrets." not in _workflow()


# --------------------------------------------------------------------------- #
# Row: Python matrix (and that the matrix value is actually consumed)
# --------------------------------------------------------------------------- #


def test_python_matrix_covers_3_12_and_3_14() -> None:
    """Row: Python matrix. The job strategy matrix contains both "3.12" and "3.14"
    (CLAUDE.md §5)."""
    matrix = re.search(r"matrix:\s*\n\s*python-version:\s*(\[[^\]]*\])", _workflow())
    assert matrix, "no strategy.matrix.python-version list"
    assert '"3.12"' in matrix.group(1)
    assert '"3.14"' in matrix.group(1)


def test_matrix_python_version_is_actually_consumed_by_both_steps() -> None:
    """AC "pytest runs on Python 3.12 and 3.14": deleting either the ``setup-uv``
    ``python-version:`` input or the ``--python`` flag on the run step would collapse the
    matrix to one interpreter. Assert the matrix value reaches the ``setup-uv`` step
    *and* the ``pytest`` run step."""
    text = _workflow()
    interp = "python-version: ${{ matrix.python-version }}"
    assert interp in text, "setup-uv is not passed the matrix interpreter"
    assert "--python ${{ matrix.python-version }}" in text, "the run step does not pin the interpreter"
    # the with: python-version input belongs to the setup-uv step, before the run step
    assert text.index("astral-sh/setup-uv@") < text.index(interp) < text.index("- run:")


def test_suite_runs_against_the_committed_lockfile() -> None:
    """Frozen Always: each leg runs the suite with the committed lockfile respected."""
    assert re.search(r"uv run --frozen .*pytest", _workflow())


# --------------------------------------------------------------------------- #
# Row: Dependabot shape
# --------------------------------------------------------------------------- #


def test_dependabot_watches_uv_and_github_actions_weekly() -> None:
    """Row: Dependabot shape. ``version: 2``; two updates -- ``uv`` and
    ``github-actions`` -- each ``directory: "/"``, weekly, ``open-pull-requests-limit: 5``."""
    text = _dependabot()
    assert re.search(r"^version:\s*2\s*$", text, re.MULTILINE)
    assert 'package-ecosystem: "uv"' in text
    assert 'package-ecosystem: "github-actions"' in text
    assert text.count("package-ecosystem:") == 2
    assert text.count('directory: "/"') == 2
    assert text.count('interval: "weekly"') == 2
    assert text.count("open-pull-requests-limit: 5") == 2


# --------------------------------------------------------------------------- #
# No new dependency was introduced by these config-assertion tests
# --------------------------------------------------------------------------- #


def test_no_yaml_parser_is_importable_or_declared() -> None:
    """Frozen Never: no new runtime or dev dependency. No YAML parser is importable
    (transitively included), and neither ``pyyaml`` nor ``ruamel`` is declared in the
    project's dependency lists."""
    assert importlib.util.find_spec("yaml") is None, "a YAML parser is importable"

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    blocks = re.findall(
        r"^dependencies\s*=\s*\[.*?\]|^\[dependency-groups\].*?(?=^\[|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    haystack = "\n".join(blocks).lower()
    for forbidden in ("pyyaml", "ruamel"):
        assert forbidden not in haystack, f"{forbidden} declared in pyproject.toml deps"
