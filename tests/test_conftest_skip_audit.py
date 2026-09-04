"""``tests/conftest.py``'s skip-reason audit (Epic 2 retro finding B1 / action
item ``epic-2-retro-item-46``).

``pytest_sessionfinish`` cannot be unit-tested in-process — it fires once,
at the end of the very session that would be exercising it. Each row here
spins up a real, isolated ``python -m pytest`` subprocess against a copy of
the actual ``tests/conftest.py`` (not a re-typed stand-in, so this cannot
drift from what really ships) plus one synthetic test module, and asserts on
the subprocess's exit code and terminal output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST_SRC = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")


def _run_isolated(tmp_path: Path, test_body: str) -> subprocess.CompletedProcess:
    (tmp_path / "conftest.py").write_text(CONFTEST_SRC, encoding="utf-8")
    (tmp_path / "test_synthetic.py").write_text(test_body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )


def test_a_known_skip_reason_passes_the_audit(tmp_path: Path) -> None:
    result = _run_isolated(
        tmp_path,
        'import pytest\n\n\ndef test_x():\n    pytest.skip("pending Epic 99")\n',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unaccounted skip" not in result.stdout


def test_an_unregistered_skip_reason_fails_the_session(tmp_path: Path) -> None:
    result = _run_isolated(
        tmp_path,
        'import pytest\n\n\ndef test_x():\n    pytest.skip("a brand new capability gate")\n',
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "unaccounted skip" in result.stdout
    assert "a brand new capability gate" in result.stdout


def test_an_otherwise_green_run_with_an_unregistered_skip_still_fails(
    tmp_path: Path,
) -> None:
    """The mechanism this guards against exactly: every other test passes, so
    a plain pass/fail count alone would read the run as healthy."""
    result = _run_isolated(
        tmp_path,
        (
            "import pytest\n\n\n"
            "def test_passes():\n    assert True\n\n\n"
            'def test_unregistered_skip():\n    pytest.skip("some new gate")\n'
        ),
    )
    assert "1 passed" in result.stdout
    assert result.returncode != 0, result.stdout + result.stderr
