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
README = REPO_ROOT / "README.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

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


def _readme() -> str:
    """Return ``README.md`` text, or ``pytest.skip`` when this tree has no
    ``README.md`` -- mirrors ``_read_github_file``'s out-of-tree skip."""
    if not README.is_file():
        pytest.skip("no README.md in this tree -- repo-hygiene check, not library behavior")
    return README.read_text(encoding="utf-8")


def _contributing() -> str:
    """Return ``CONTRIBUTING.md`` text, or ``pytest.skip`` when this tree has no
    ``CONTRIBUTING.md`` -- mirrors ``_read_github_file``'s out-of-tree skip."""
    if not CONTRIBUTING.is_file():
        pytest.skip("no CONTRIBUTING.md in this tree -- repo-hygiene check, not library behavior")
    return CONTRIBUTING.read_text(encoding="utf-8")


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


def _permissions_blocks(text: str) -> list[tuple[str, str]]:
    """``(label, body)`` for **every** ``permissions:`` key at any indent -- ``label`` is
    ``"top-level"`` (column 0) or ``"job-level"`` (indented, e.g. under ``jobs.<id>:``).
    ``body`` is the inline value or the space-joined nested entries.

    retro A1(a): ``_top_level_permissions`` anchors ``^permissions:`` at column 0, so a
    job-level block was literally unmatchable -- yet GitHub merges top-level and job-level
    ``permissions:`` and a job block overrides. This closes that bypass by scanning both.
    """
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)permissions:\s*(.*?)\s*(?:#.*)?$", ln)
        if not m:
            continue
        indent = len(m.group(1))
        label = "top-level" if indent == 0 else "job-level"
        inline = m.group(2).strip()
        # P1: an inline value that is a block scalar (``|`` / ``>``), a YAML ``&anchor``,
        # or an unclosed flow map (``{`` without its ``}``) continues onto the indented
        # lines below -- take BOTH so a ``contents: write`` on the next line is not missed.
        continues_below = bool(inline) and (
            inline[0] in "|>&" or inline.count("{") > inline.count("}")
        )
        if inline and not continues_below:
            blocks.append((label, inline))
            continue
        body: list[str] = [inline] if inline else []
        for sub in lines[i + 1:]:
            if not sub.strip() or sub.lstrip().startswith("#"):
                continue
            if len(sub) - len(sub.lstrip()) <= indent:  # dedent to <= key indent ends it
                break
            body.append(sub.strip())
        blocks.append((label, " ".join(body)))
    return blocks


def _least_privilege_violations(text: str) -> list[str]:
    """Every way a workflow's ``permissions:`` blocks break least privilege -- shared by
    the over-every-workflow guard and its committed tripwire so the two cannot drift.
    retro A1(a) + P1: scans top-level AND job-level blocks; catches ``write-all``, a bare
    ``<scope>: write``, and a *quoted* ``<scope>: "write"`` / ``'write'``."""
    out: list[str] = []
    blocks = _permissions_blocks(text)
    if not any(label == "top-level" for label, _ in blocks):
        out.append("no top-level permissions: block")
    for label, body in blocks:
        if "write-all" in body:
            out.append(f"{label} permissions: write-all")
        for scope, mode in re.findall(
            r"([A-Za-z-]+):\s*['\"]?(read|write|none)['\"]?\b", body
        ):
            if mode == "write":
                out.append(f"{label} permissions grants {scope}: write")
    return out


def _secret_violations(text: str) -> list[str]:
    """Every repository-secret reference beyond the auto-provisioned ``GITHUB_TOKEN``
    (dot or index form, any inner whitespace). retro A1(b) + P2: also rejects
    ``toJSON(secrets)`` (serializes every secret), ``secrets: inherit`` (reusable-workflow
    passthrough), and a bare ``${{ secrets }}``. Shared by the guard and its tripwire."""
    leftover = re.sub(r"secrets\s*\.\s*GITHUB_TOKEN\b", "", text)
    leftover = re.sub(r"secrets\s*\[\s*['\"]GITHUB_TOKEN['\"]\s*\]", "", leftover)
    out: list[str] = []
    if re.search(r"secrets\s*\.", leftover):
        out.append("references a repository secret (dot form)")
    if re.search(r"secrets\s*\[", leftover):
        out.append("references a repository secret (index form)")
    if re.search(r"toJSON\(\s*secrets\b", leftover):
        out.append("toJSON(secrets) serializes every repository secret")
    if re.search(r"secrets\s*:\s*inherit\b", leftover):
        out.append("secrets: inherit passes every secret to a reusable workflow")
    if re.search(r"\bsecrets\s*}}", leftover):
        out.append("bare ${{ secrets }} expression")
    return out


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
    ``permissions:`` block, and no block -- top-level OR job-level -- grants
    ``write-all`` or any ``<scope>: write``.

    retro A1(a): the least-privilege check now inspects every job-level ``permissions:``
    block too (any indent), not just the column-0 one. GitHub honours a job-level block
    and it overrides the top-level grant, so a hostile contributed workflow with a benign
    top-level block and a ``contents: write`` job block previously passed clean.
    """
    for name, text in _all_workflows():
        violations = _least_privilege_violations(text)
        assert not violations, f"{name}: {violations}"


def test_least_privilege_permissions_tripwire() -> None:
    """Committed regression test for retro A1(a) / P1. The over-every-workflow guard sees
    no hostile ``permissions:`` on a clean tree, so feed hand-written workflow text
    straight to ``_least_privilege_violations`` -- mirrors ``test_count_impls_helper_tripwire``."""
    top = "on: push\npermissions:\n  contents: read\n"

    # a clean top-level-only shape (today's test.yml) is accepted
    assert _least_privilege_violations(top + "jobs:\n  test:\n    runs-on: x\n    steps: []\n") == []

    # job-level bare write
    v = _least_privilege_violations(top + "jobs:\n  b:\n    permissions:\n      contents: write\n")
    assert any("job-level" in x and "contents: write" in x for x in v), v

    # job-level write-all
    v = _least_privilege_violations(top + "jobs:\n  b:\n    permissions: write-all\n")
    assert any("write-all" in x for x in v), v

    # job-level QUOTED write (block form and inline flow form)
    v = _least_privilege_violations(top + 'jobs:\n  b:\n    permissions:\n      contents: "write"\n')
    assert any("contents: write" in x for x in v), v
    v = _least_privilege_violations(top + "jobs:\n  b:\n    permissions: {contents: 'write'}\n")
    assert any("contents: write" in x for x in v), v

    # multi-line flow map continues onto indented lines
    v = _least_privilege_violations(
        top + "jobs:\n  b:\n    permissions: {\n      contents: write,\n    }\n"
    )
    assert any("contents: write" in x for x in v), v

    # a workflow with NO top-level block at all is rejected
    assert _least_privilege_violations("on: push\njobs:\n  b:\n    steps: []\n") == [
        "no top-level permissions: block"
    ]


def test_no_workflow_uses_pull_request_target() -> None:
    """Row / frozen Never: ``pull_request_target`` appears in no workflow."""
    for name, text in _all_workflows():
        assert "pull_request_target" not in text, f"{name}: uses pull_request_target"


def test_no_workflow_references_a_repository_secret_other_than_github_token() -> None:
    """Frozen Never / CLAUDE.md §4: no secret exposed to a fork-triggered workflow. Only
    the auto-provisioned ``secrets.GITHUB_TOKEN`` is permitted anywhere.

    retro A1(b) + P2: matches the index form ``secrets['NAME']`` / ``secrets["NAME"]``
    (any inner whitespace) alongside ``secrets.NAME``, and also rejects
    ``${{ toJSON(secrets) }}``, ``secrets: inherit``, and a bare ``${{ secrets }}`` --
    each leaks every repository secret. Only ``GITHUB_TOKEN`` is allowed, in either form.
    """
    for name, text in _all_workflows():
        violations = _secret_violations(text)
        assert not violations, f"{name}: {violations}"


def test_secret_scan_tripwire() -> None:
    """Committed regression test for retro A1(b) / P2 -- the guard sees no secret on a
    clean tree, so feed hand-written snippets to ``_secret_violations`` directly."""
    for snippet in (
        "run: echo ${{ secrets['PYPI_TOKEN'] }}",
        'run: echo ${{ secrets["PYPI_TOKEN"] }}',
        "run: echo ${{ secrets.PYPI_TOKEN }}",
        "run: echo ${{ secrets . PYPI_TOKEN }}",
        "run: echo ${{ toJSON(secrets) }}",
        "run: echo ${{ toJSON( secrets ) }}",
        "    secrets: inherit",
        "run: echo ${{ secrets }}",
    ):
        assert _secret_violations(snippet), f"not rejected: {snippet!r}"

    for snippet in (
        "run: echo ${{ secrets.GITHUB_TOKEN }}",
        "run: echo ${{ secrets['GITHUB_TOKEN'] }}",
        'run: echo ${{ secrets["GITHUB_TOKEN"] }}',
        "run: echo ${{ secrets . GITHUB_TOKEN }}",
        "run: echo nothing sensitive here",
    ):
        assert not _secret_violations(snippet), f"wrongly rejected: {snippet!r}"

    if (REPO_ROOT / ".github").is_dir():
        assert _secret_violations(WORKFLOW.read_text(encoding="utf-8")) == []


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
    pytest_run = "- run: uv run --frozen"
    assert interp in text, "setup-uv is not passed the matrix interpreter"
    assert "--python ${{ matrix.python-version }}" in text, "the run step does not pin the interpreter"
    # the with: python-version input belongs to the setup-uv step, before the pytest run
    # step -- anchored on the pytest step's own text (not the generic "- run:", which
    # this story's added `uv lock --check` step now also matches, first)
    assert text.index("astral-sh/setup-uv@") < text.index(interp) < text.index(pytest_run)


def test_suite_runs_against_the_committed_lockfile() -> None:
    """Frozen Always: each leg runs the suite with the committed lockfile respected."""
    assert re.search(r"uv run --frozen .*pytest", _workflow())


def test_python_matrix_also_covers_3_13() -> None:
    """AC: the matrix contains "3.13" alongside "3.12"/"3.14" (Story 1B.1 -- chosen
    over a floor-plus-latest rationale note)."""
    matrix = re.search(r"matrix:\s*\n\s*python-version:\s*(\[[^\]]*\])", _workflow())
    assert matrix, "no strategy.matrix.python-version list"
    assert '"3.13"' in matrix.group(1)


# --------------------------------------------------------------------------- #
# Row: Lint / type-check gate (Story 1B.1)
# --------------------------------------------------------------------------- #


def test_lint_job_present_and_runs_ruff_and_mypy() -> None:
    """AC: given ``[tool.ruff]``/``[tool.mypy]`` are defined, the ``lint`` job runs
    both, so a reintroduced violation fails CI."""
    text = _workflow()
    assert re.search(r"^  lint:\s*$", text, re.MULTILINE), "no top-level lint: job"
    assert re.search(r"uv run --frozen ruff check", text), "lint job does not run ruff"
    assert re.search(r"uv run --frozen mypy commishdesk", text), "lint job does not run mypy"


def test_lint_job_is_declared_after_the_test_job() -> None:
    """Frozen Always: ``lint:`` is ordered after ``test:`` in the YAML so
    ``test_matrix_python_version_is_actually_consumed_by_both_steps``'s
    first-occurrence anchors keep resolving inside ``test:``'s own steps."""
    text = _workflow()
    assert text.index("\n  test:") < text.index("\n  lint:")


def _step_blocks(text: str, action_prefix: str) -> list[str]:
    """Text of every step whose ``uses:`` starts with *action_prefix*, from its
    ``uses:`` line up to (not including) the next step at the same indent or the
    end of its job. Mirrors ``_permissions_blocks``'s indent-based extraction."""
    lines = text.splitlines()
    blocks: list[str] = []
    for i, ln in enumerate(lines):
        m = re.match(rf"^(\s*)-\s*uses:\s*{re.escape(action_prefix)}", ln)
        if not m:
            continue
        indent = len(m.group(1))
        chunk = [ln]
        for sub in lines[i + 1 :]:
            stripped = sub.lstrip()
            sub_indent = len(sub) - len(stripped)
            if not stripped:
                chunk.append(sub)
                continue
            if sub_indent <= indent:  # next step, or dedent out of this job
                break
            chunk.append(sub)
        blocks.append("\n".join(chunk))
    return blocks


def test_every_checkout_step_sets_persist_credentials_false() -> None:
    """Row: Fork PR. Every ``actions/checkout`` step disables credential
    persistence so no token is left on disk for a later step to exfiltrate."""
    blocks = _step_blocks(_workflow(), "actions/checkout@")
    assert blocks, "no actions/checkout step found"
    for block in blocks:
        assert "persist-credentials: false" in block, block


def test_every_setup_uv_step_pins_a_version_and_enables_cache() -> None:
    """AC: ``setup-uv`` carries a pinned ``version:`` input (the uv binary version)
    and ``enable-cache: true``, on every job that uses it."""
    blocks = _step_blocks(_workflow(), "astral-sh/setup-uv@")
    assert blocks, "no astral-sh/setup-uv step found"
    for block in blocks:
        # negative lookbehind for a preceding word/hyphen char so this matches only the
        # uv-binary `version:` key, not a suffix of `python-version:` (which is quoted
        # in the `lint` job's setup-uv step, just like a real `version:` pin would be).
        assert re.search(r"(?<![\w-])version:\s*['\"][\w.]+['\"]", block), block
        assert "enable-cache: true" in block, block


def test_uv_lock_check_step_present() -> None:
    """AC: a ``uv lock --check`` step exists, so a stale lockfile fails the build."""
    assert re.search(r"-\s*run:\s*uv lock --check", _workflow())


def _job_blocks(text: str) -> dict[str, str]:
    """``{job_id: block_text}`` for each top-level job under ``jobs:`` (2-space
    indent keys)."""
    lines = text.splitlines()
    jobs_start = next(
        (i + 1 for i, ln in enumerate(lines) if re.match(r"^jobs:\s*$", ln)), None
    )
    assert jobs_start is not None, "no jobs: key"
    blocks: dict[str, str] = {}
    current_id: str | None = None
    current_lines: list[str] = []
    for ln in lines[jobs_start:]:
        m = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", ln)
        if m:
            if current_id is not None:
                blocks[current_id] = "\n".join(current_lines)
            current_id, current_lines = m.group(1), []
            continue
        if current_id is not None:
            current_lines.append(ln)
    if current_id is not None:
        blocks[current_id] = "\n".join(current_lines)
    return blocks


def test_every_job_declares_a_timeout_minutes() -> None:
    """AC: ``timeout-minutes`` is present on every job -- a hung runner cannot pin
    the queue indefinitely."""
    jobs = _job_blocks(_workflow())
    assert jobs, "no jobs found"
    for job_id, block in jobs.items():
        assert re.search(r"^\s*timeout-minutes:\s*\d+", block, re.MULTILINE), job_id


def test_no_job_runs_on_a_bare_ubuntu_latest() -> None:
    """AC: a pinned runner image, not the floating ``ubuntu-latest`` alias."""
    text = _workflow()
    assert "ubuntu-latest" not in text
    assert re.search(r"runs-on:\s*ubuntu-\d", text), "no pinned ubuntu-NN.NN image"


def test_cancel_in_progress_is_gated_to_pull_request() -> None:
    """Row: Rapid pushes to main. ``cancel-in-progress`` is conditioned on
    ``github.event_name == 'pull_request'`` so a post-merge ``push`` run to
    ``main`` is never cancelled by the next commit."""
    text = _workflow()
    assert re.search(
        r"cancel-in-progress:\s*\$\{\{\s*github\.event_name\s*==\s*'pull_request'\s*\}\}",
        text,
    ), "cancel-in-progress is not gated to pull_request"


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


def _dependabot_entries(text: str) -> list[str]:
    """Text of each top-level ``updates`` entry, from its own ``- package-ecosystem:``
    line up to the next one (or EOF). Per-entry isolation so a copy-paste bug that
    attaches both ecosystems' ``groups:``/``commit-message:``/``labels:`` blocks under
    one entry -- leaving the other bare -- cannot pass a whole-file ``text.count(...)``
    check that only cares about the file-wide total."""
    parts = re.split(r"(?=^  - package-ecosystem:)", text, flags=re.MULTILINE)
    return [p for p in parts if p.lstrip().startswith("- package-ecosystem:")]


def test_dependabot_groups_commit_message_and_labels_per_ecosystem() -> None:
    """Row: Two same-ecosystem deps bump in one week / Dependabot opens a PR
    (Story 1B.2). EVERY ``updates`` entry -- checked individually, not as a file-wide
    count -- groups same-ecosystem bumps into one PR, prefixes its commit message, and
    carries the ``dependencies`` label."""
    entries = _dependabot_entries(_dependabot())
    assert len(entries) == 2, f"expected exactly 2 update entries, found {len(entries)}"
    for entry in entries:
        assert "groups:" in entry, f"missing groups: block in entry:\n{entry}"
        assert 'patterns: ["*"]' in entry, f"missing same-ecosystem grouping pattern in entry:\n{entry}"
        assert "commit-message:" in entry, f"missing commit-message: block in entry:\n{entry}"
        assert 'prefix: "chore(deps)"' in entry, f"missing chore(deps) prefix in entry:\n{entry}"
        assert "labels:" in entry, f"missing labels: block in entry:\n{entry}"
        assert '- "dependencies"' in entry, f"missing dependencies label in entry:\n{entry}"


# --------------------------------------------------------------------------- #
# Row: README CI badge (Story 1B.2)
# --------------------------------------------------------------------------- #


def test_readme_shows_ci_badge_for_test_workflow() -> None:
    """AC: README.md displays a CI status badge for the ``test`` workflow, and the
    badge image and its link target form one coherent Markdown image-link -- not two
    unrelated occurrences of a badge URL and a workflow URL elsewhere on the page --
    pointing at that workflow's own Actions page (not ``lint``, a job inside it)."""
    text = _readme()
    assert re.search(
        r"\[!\[[^\]]*\]\(https://github\.com/Eric-Weng/CommishDesk/actions/workflows/"
        r"test\.yml/badge\.svg\)\]\(https://github\.com/Eric-Weng/CommishDesk/actions/"
        r"workflows/test\.yml\)",
        text,
    ), "no GitHub Actions workflow badge for the test workflow, linked to its Actions page"


# --------------------------------------------------------------------------- #
# Row: a future DCO gate lands / dependabot[bot] exemption (Story 1B.2)
# --------------------------------------------------------------------------- #


def test_contributing_documents_dependabot_dco_exemption() -> None:
    """Row: A future DCO gate lands / dependabot[bot]-authored commit -- CONTRIBUTING.md's
    DCO section documents that ``dependabot[bot]`` commits are exempt from sign-off, so a
    later automated DCO check (not yet built) can allowlist that actor instead of failing
    every Dependabot PR."""
    text = _contributing()
    dco_heading = re.search(r"^## .*DCO.*$", text, re.MULTILINE)
    assert dco_heading, "no DCO section in CONTRIBUTING.md"
    assert "dependabot[bot]" in text, "no dependabot[bot] exemption documented"
    # The exemption line must live in (or after) the DCO section, not somewhere unrelated.
    assert text.index("dependabot[bot]") > dco_heading.start(), "exemption not documented in the DCO section"


# --------------------------------------------------------------------------- #
# Row: PyPI listing rendered -- [project] release-readiness metadata (Story 1B.2)
# --------------------------------------------------------------------------- #


_QUOTED_NONEMPTY = re.compile(r'"[^"]+"')  # a quoted string with at least one char inside


def test_project_table_has_release_readiness_metadata() -> None:
    """AC: given ``pyproject.toml``'s ``[project]`` table, when read, then it carries
    ``urls`` (repository, issues), ``classifiers``, ``keywords``, and name-only
    ``authors`` (no email -- CLAUDE.md §1: no real email address anywhere in this repo).

    Each array/value is checked for actual content, not just key presence -- an empty
    ``keywords = []`` / ``classifiers = []`` or a blank ``Repository = ""`` would pass a
    bare ``re.search(r"^keywords\\s*=", ...)`` or substring check but is not, in fact,
    release-readiness metadata."""
    text = PYPROJECT.read_text(encoding="utf-8")

    project_block = re.search(r"^\[project\]\n.*?(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert project_block, "no [project] table"
    block = project_block.group(0)

    authors_line = re.search(r"^authors\s*=.*$", block, re.MULTILINE)
    assert authors_line, "no authors in [project]"
    assert "@" not in authors_line.group(0), "authors carries an email address"

    keywords_m = re.search(r"^keywords\s*=\s*(\[.*?\])", block, re.MULTILINE | re.DOTALL)
    assert keywords_m, "no keywords in [project]"
    assert _QUOTED_NONEMPTY.search(keywords_m.group(1)), "keywords array is empty"

    classifiers_m = re.search(r"^classifiers\s*=\s*(\[.*?\])", block, re.MULTILINE | re.DOTALL)
    assert classifiers_m, "no classifiers in [project]"
    assert _QUOTED_NONEMPTY.search(classifiers_m.group(1)), "classifiers array is empty"

    urls_block = re.search(r"^\[project\.urls\]\n.*?(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert urls_block, "no [project.urls] table"

    repo_m = re.search(r'^Repository\s*=\s*"(.*?)"', urls_block.group(0), re.MULTILINE)
    assert repo_m, "no Repository url"
    assert repo_m.group(1).strip(), "Repository url is empty"

    issues_m = re.search(r'^Issues\s*=\s*"(.*?)"', urls_block.group(0), re.MULTILINE)
    assert issues_m, "no Issues url"
    assert issues_m.group(1).strip(), "Issues url is empty"


# --------------------------------------------------------------------------- #
# No new dependency was introduced by these config-assertion tests
# --------------------------------------------------------------------------- #


def _declared_dependency_blocks(pyproject_text: str) -> str:
    """Lowercased text of every ``pyproject.toml`` table that can introduce an installable
    dependency: the whole ``[project]`` table (its ``dependencies`` array), the whole
    ``[dependency-groups]`` table, and the whole ``[project.optional-dependencies]`` table
    (retro A4). retro P3: whole tables are captured -- the old ``dependencies = [.*?]``
    stopped at the first ``]``, so a requirement with an extras bracket (``typer[all]``)
    truncated the block and hid every dependency after it."""
    blocks = re.findall(
        r"^\[project\]\n.*?(?=^\[|\Z)"
        r"|^\[dependency-groups\]\n.*?(?=^\[|\Z)"
        r"|^\[project\.optional-dependencies\]\n.*?(?=^\[|\Z)",
        pyproject_text,
        re.MULTILINE | re.DOTALL,
    )
    return "\n".join(blocks).lower()


def test_no_yaml_parser_is_importable_or_declared() -> None:
    """Frozen Never: no new runtime or dev dependency. No YAML parser is importable
    (transitively included), and neither ``pyyaml`` nor ``ruamel`` is declared in the
    project's dependency lists.

    retro A4: the scan now also covers ``[project.optional-dependencies]``.
    retro P3: ``ruamel`` imports as ``ruamel.yaml`` (invisible to ``find_spec("yaml")``)
    so it gets its own import check, and the table capture no longer truncates on ``[``.
    """
    assert importlib.util.find_spec("yaml") is None, "a YAML parser is importable"
    assert importlib.util.find_spec("ruamel") is None, "ruamel.yaml is importable"

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    haystack = _declared_dependency_blocks(text)
    for forbidden in ("pyyaml", "ruamel"):
        assert forbidden not in haystack, f"{forbidden} declared in pyproject.toml deps"


def test_declared_dependency_scan_tripwire() -> None:
    """Committed regression test for retro A4 / P3 -- the guard sees a clean pyproject on
    this tree, so feed hand-written pyproject text to ``_declared_dependency_blocks``."""
    clean = (
        '[project]\nname = "x"\n'
        'dependencies = ["httpx>=0.27", "typer[all]>=0.12", "pydantic>=2"]\n\n'
        "[build-system]\nrequires = []\n"
    )

    def _hit(pyproject_text: str) -> bool:
        hay = _declared_dependency_blocks(pyproject_text)
        return any(f in hay for f in ("pyyaml", "ruamel"))

    assert not _hit(clean)
    # truncation regression: a bracket in an earlier spec must not hide a later dep
    assert _hit(
        '[project]\ndependencies = ["typer[all]", "pyyaml"]\n\n[build-system]\nrequires = []\n'
    )
    assert _hit(clean + '\n[project.optional-dependencies]\nextra = ["pyyaml"]\n')
    assert _hit(clean + '\n[dependency-groups]\ndev = ["pytest", "ruamel.yaml"]\n')
