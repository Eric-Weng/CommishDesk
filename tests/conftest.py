"""Session-wide test infrastructure.

Currently holds only the skip-reason audit (Epic 2 retro finding B1 / action
item ``epic-2-retro-item-46``). Deliberately minimal: the shared ``REPO_ROOT``
/ ``_find_uv`` consolidation (finding B2 / action item ``...-47``) is a
separate, larger cleanup and is not folded in here.
"""

from __future__ import annotations

from typing import Any

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
