"""Session-wide test infrastructure.

Holds the skip-reason audit (Epic 2 retro finding B1 / action item
``epic-2-retro-item-46``) and the shared ``REPO_ROOT`` constant (Epic 4
retro item 70 / finding B2 / action item ``...-47``): every test file that
previously defined its own ``REPO_ROOT`` now imports it from here instead
of redefining it locally. (A few files compute their own differently-named,
locally-scoped path constants derived from ``__file__`` -- e.g.
``ENGINE_ROOT``, ``PYPROJECT`` -- those were never literally ``REPO_ROOT``
and are out of scope for this cleanup.) ``_find_uv`` (duplicated in only two
files) also remains a separate, out-of-scope cleanup.

Importing ``REPO_ROOT`` as ``from tests.conftest import REPO_ROOT`` relies
on ``tests/`` being an importable package (see ``tests/__init__.py``); if
that file were ever removed, every importing test file would fail at
collection with a nonobvious root cause.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The repository root, resolved once here and imported by every test file
#: that needs it (``from tests.conftest import REPO_ROOT``) instead of each
#: file redefining ``Path(__file__).resolve().parent.parent`` itself.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The exact prefix of every ``pytest.skip(...)`` reason this suite is known
#: to emit, one per capability gate. A skip whose reason does not start with
#: one of these is unaccounted for -- either a new capability gate landed
#: without being registered here, or an existing gate's message drifted --
#: and fails the session instead of silently shrinking what CI verifies
#: (the epic gate that motivated this: CI was quietly running 7 fewer tests
#: than a local checkout, unnoticed, for the whole of Epic 2).
_KNOWN_SKIP_PREFIXES = (
    "pending Epic ",  # test_invariants.py -- intentional, not-yet-implemented
    "no .github/ in this tree",  # test_ci_config.py -- repo-hygiene, out-of-tree
    "uv not available",  # test_extension_zones.py / test_skeleton.py
    "phase-0 golden is a private planning artifact",  # test_facts.py
    "private raw Sleeper export not present",  # test_fixtures.py
    "COMMISHDESK_LIVE_LLM is unset",  # test_voices.py -- opt-in live voice eval
)

#: The ``pytest.skip(...)`` reason string in a skip report's ``longrepr`` is
#: always prefixed exactly this way -- see pytest's own skip reporting.
_LONGREPR_SKIP_PREFIX = "Skipped: "


def _skip_reason(longrepr: Any) -> str | None:
    """The bare skip reason from a ``TestReport.longrepr``, or ``None`` when
    the shape isn't the ``(path, lineno, "Skipped: ...")`` tuple pytest emits
    for an ordinary ``pytest.skip()`` -- treated as unaccounted rather than
    silently ignored, since an unrecognized shape is itself worth seeing."""
    if not (isinstance(longrepr, tuple) and len(longrepr) == 3):
        return None
    reason = longrepr[2]
    if not isinstance(reason, str) or not reason.startswith(_LONGREPR_SKIP_PREFIX):
        return None
    return reason[len(_LONGREPR_SKIP_PREFIX) :]


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is None:  # pragma: no cover - e.g. pytest -p no:terminal
        return
    unexpected: list[str] = []
    for report in terminalreporter.stats.get("skipped", []):
        reason = _skip_reason(getattr(report, "longrepr", None))
        if reason is None or not reason.startswith(_KNOWN_SKIP_PREFIXES):
            unexpected.append(f"{report.nodeid}: {reason!r}")
    if unexpected:
        terminalreporter.write_sep(
            "=", "unaccounted skip reason(s) -- see tests/conftest.py", red=True
        )
        for line in unexpected:
            terminalreporter.write_line(f"  {line}")
        session.exitstatus = 1
