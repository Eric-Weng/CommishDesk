"""Story 4.4 — ``send_issue`` + ``SendReport`` (the channel-agnostic Send Ledger).

One test per I/O & Edge-Case row: first send, an idempotent re-run, a crash
resume, per-recipient isolation, a deliberate re-issue, channel / week isolation,
a propagating ``StoreError`` from the append, empty recipients, and an
out-of-range week. Plus sorted-order determinism, an injected ``now`` reaching
``sent_at``, the default ``now`` being tz-aware UTC, the pre-send guards (naive
``now``, unsafe ``league_id``, blank ``reason``), a single run-wide ``sent_at``,
and the AD-1 import fence.
"""

from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commishdesk.deliver import SendReport, send_issue
from commishdesk.errors import DeliveryError, StoreError
from commishdesk.store import FileStore, LedgerEntry

REPO_ROOT = Path(__file__).resolve().parent.parent

_FIXED_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _now() -> datetime:
    return _FIXED_NOW


def _store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path)


def _seed(store: FileStore, **overrides: object) -> None:
    """Append one ``confirmed`` ledger entry (``test_store.py`` pattern)."""
    fields: dict[str, object] = dict(
        league_id="42",
        week=3,
        channel="discord",
        recipient="42",
        sent_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    fields.update(overrides)
    store.append_ledger_entry(LedgerEntry(**fields))  # type: ignore[arg-type]


class _Recorder:
    """A fake ``sender`` that records every call and can raise for chosen recipients."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail or set()

    def __call__(self, recipient: str, content: str) -> None:
        self.calls.append((recipient, content))
        if recipient in self._fail:
            raise DeliveryError(f"channel hiccup for {recipient}")


# --------------------------------------------------------------------------- #
# I/O & Edge-Case Matrix
# --------------------------------------------------------------------------- #


def test_first_send_calls_sender_once_and_appends_one_confirmed_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"42": "body"},
        sender=sender,
        now=_now,
    )
    assert sender.calls == [("42", "body")]
    assert report == SendReport(delivered=("42",))

    entries = store.read_ledger("42", 3)
    assert len(entries) == 1
    entry = entries[0]
    assert (entry.league_id, entry.week, entry.channel, entry.recipient) == (
        "42",
        3,
        "discord",
        "42",
    )
    assert entry.status == "confirmed"
    assert entry.sent_at == _FIXED_NOW
    assert entry.reason is None


def test_second_identical_run_skips_and_appends_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)  # confirmed (discord, "42") for league 42 week 3
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"42": "body"},
        sender=sender,
        now=_now,
    )
    assert sender.calls == []
    assert report == SendReport(skipped=("42",))
    assert len(store.read_ledger("42", 3)) == 1


def test_partial_resume_only_sends_the_unconfirmed_recipient(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, recipient="42")
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"42": "a", "43": "b"},
        sender=sender,
        now=_now,
    )
    assert sender.calls == [("43", "b")]
    assert report.delivered == ("43",)
    assert report.skipped == ("42",)
    assert report.failed == ()
    assert sorted(e.recipient for e in store.read_ledger("42", 3)) == ["42", "43"]


def test_one_recipient_failing_still_sends_and_confirms_the_rest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sender = _Recorder(fail={"1"})
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"1": "x", "2": "y"},
        sender=sender,
        now=_now,
    )
    assert sender.calls == [("1", "x"), ("2", "y")]  # loop continued past the failure
    assert report.delivered == ("2",)
    assert len(report.failed) == 1
    failed_recipient, message = report.failed[0]
    assert failed_recipient == "1"
    assert "1" in message and "\n" not in message
    # the failed recipient has no ledger entry
    assert [e.recipient for e in store.read_ledger("42", 3)] == ["2"]


def test_reason_re_sends_a_confirmed_recipient_and_writes_a_new_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, recipient="42")
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"42": "corrected"},
        sender=sender,
        now=_now,
        reason="correction",
    )
    assert sender.calls == [("42", "corrected")]
    assert report.delivered == ("42",)
    entries = store.read_ledger("42", 3)
    assert len(entries) == 2
    assert entries[-1].reason == "correction"
    assert entries[-1].sent_at == _FIXED_NOW


def test_a_confirmed_recipient_on_another_channel_is_not_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, channel="email", recipient="a@b")
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"a@b": "body"},
        sender=sender,
        now=_now,
    )
    assert sender.calls == [("a@b", "body")]
    assert report.delivered == ("a@b",)
    assert [(e.channel, e.recipient) for e in store.read_ledger("42", 3)] == [
        ("email", "a@b"),
        ("discord", "a@b"),
    ]


def test_a_confirmed_recipient_in_another_week_is_not_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, week=3, recipient="42")
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=4,
        channel="discord",
        recipients={"42": "body"},
        sender=sender,
        now=_now,
    )
    assert sender.calls == [("42", "body")]
    assert report.delivered == ("42",)
    assert [e.week for e in store.read_ledger("42", 4)] == [4]


def test_store_error_from_the_append_propagates(tmp_path: Path) -> None:
    class _BadAppend(FileStore):
        def append_ledger_entry(self, entry: LedgerEntry) -> None:
            raise StoreError("ledger append failed")

    store = _BadAppend(tmp_path)
    sender = _Recorder()
    with pytest.raises(StoreError):
        send_issue(
            store,
            league_id="42",
            week=3,
            channel="discord",
            recipients={"42": "body"},
            sender=sender,
            now=_now,
        )
    assert sender.calls == [("42", "body")]  # the send did happen first


def test_empty_recipients_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={},
        sender=sender,
        now=_now,
    )
    assert sender.calls == []
    assert report == SendReport()
    assert store.read_ledger("42", 3) == []


@pytest.mark.parametrize("bad_week", [0, 25, -1, 19])
def test_out_of_range_week_raises_before_any_send(tmp_path: Path, bad_week: int) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    with pytest.raises(ValueError):
        send_issue(
            store,
            league_id="42",
            week=bad_week,
            channel="discord",
            recipients={"42": "body"},
            sender=sender,
            now=_now,
        )
    assert sender.calls == []
    assert store.read_ledger("42", 1) == []


@pytest.mark.parametrize("good_week", [1, 18])
def test_week_range_boundaries_are_accepted(tmp_path: Path, good_week: int) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=good_week,
        channel="discord",
        recipients={"42": "body"},
        sender=sender,
        now=_now,
    )
    assert report.delivered == ("42",)
    assert [e.week for e in store.read_ledger("42", good_week)] == [good_week]


# --------------------------------------------------------------------------- #
# Pre-send guards
# --------------------------------------------------------------------------- #


def test_naive_now_raises_before_any_send(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    with pytest.raises(ValueError, match="timezone-aware"):
        send_issue(
            store,
            league_id="42",
            week=3,
            channel="discord",
            recipients={"42": "body"},
            sender=sender,
            now=lambda: datetime(2026, 9, 10, 12, 0),  # deliberately naive — no tzinfo
        )
    assert sender.calls == []
    assert store.read_ledger("42", 3) == []


@pytest.mark.parametrize("bad_id", ["", ".", "..", "../x", "a/b", "a\\b", "a\x00b"])
def test_unsafe_league_id_raises_before_any_send(tmp_path: Path, bad_id: str) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    with pytest.raises(ValueError, match="unsafe league id"):
        send_issue(
            store,
            league_id=bad_id,
            week=3,
            channel="discord",
            recipients={"42": "body"},
            sender=sender,
            now=_now,
        )
    assert sender.calls == []


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_reason_raises_before_any_send(tmp_path: Path, blank: str) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    with pytest.raises(ValueError, match="non-blank"):
        send_issue(
            store,
            league_id="42",
            week=3,
            channel="discord",
            recipients={"42": "body"},
            sender=sender,
            now=_now,
            reason=blank,
        )
    assert sender.calls == []
    assert store.read_ledger("42", 3) == []


def test_every_entry_in_one_run_shares_one_sent_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ticks = iter(
        [
            datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 10, 12, 0, 1, tzinfo=UTC),
            datetime(2026, 9, 10, 12, 0, 2, tzinfo=UTC),
        ]
    )
    send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"1": "a", "2": "b", "3": "c"},
        sender=_Recorder(),
        now=lambda: next(ticks),
    )
    stamps = {e.sent_at for e in store.read_ledger("42", 3)}
    assert stamps == {datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)}


# --------------------------------------------------------------------------- #
# Determinism + the injected clock
# --------------------------------------------------------------------------- #


def test_recipients_are_processed_in_sorted_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sender = _Recorder()
    report = send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"3": "c", "1": "a", "2": "b"},
        sender=sender,
        now=_now,
    )
    assert [r for r, _ in sender.calls] == ["1", "2", "3"]
    assert report.delivered == ("1", "2", "3")
    assert [e.recipient for e in store.read_ledger("42", 3)] == ["1", "2", "3"]


def test_injected_now_reaches_sent_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stamp = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"42": "body"},
        sender=_Recorder(),
        now=lambda: stamp,
    )
    assert store.read_ledger("42", 3)[0].sent_at == stamp


def test_default_now_is_tz_aware_utc(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before = datetime.now(tz=UTC)
    send_issue(
        store,
        league_id="42",
        week=3,
        channel="discord",
        recipients={"42": "body"},
        sender=_Recorder(),
    )
    after = datetime.now(tz=UTC)
    sent_at = store.read_ledger("42", 3)[0].sent_at
    assert sent_at.tzinfo is not None
    assert before <= sent_at <= after


# --------------------------------------------------------------------------- #
# AD-1 import fence
# --------------------------------------------------------------------------- #


def test_ledger_module_imports_only_stdlib_and_commishdesk() -> None:
    tree = ast.parse(
        (REPO_ROOT / "commishdesk" / "deliver" / "ledger.py").read_text(encoding="utf-8")
    )
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    external = roots - sys.stdlib_module_names
    assert external <= {"commishdesk"}, external
    assert "httpx" not in roots
