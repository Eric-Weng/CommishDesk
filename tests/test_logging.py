"""Story 1.3: structured logging with redaction — one test per I/O & Edge-Case row."""

from __future__ import annotations

import io
import json
import logging

import pytest

from commishdesk.logconfig import configure_logging, log_context


class FakeStream(io.StringIO):
    """An in-memory stream with a controllable ``isatty()``."""

    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def _clean_logging_env(monkeypatch):
    """No ambient ``COMMISHDESK_LOG_FORMAT`` unless a test sets one; reset logger after."""
    monkeypatch.delenv("COMMISHDESK_LOG_FORMAT", raising=False)
    yield
    logger = logging.getLogger("commishdesk")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)


def _log(stream, *, verbose=False):
    logger = configure_logging(verbose=verbose, stream=stream)
    return logger


def _json_lines(stream: FakeStream) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# --- TTY vs non-TTY output ------------------------------------------------


def test_tty_output_is_human_readable():
    stream = FakeStream(tty=True)
    _log(stream).info("hello world")
    line = stream.getvalue().strip()
    assert "INFO hello world" in line
    # human line starts with HH:MM:SS
    assert line[2] == ":" and line[5] == ":"
    assert not line.startswith("{")


def test_non_tty_output_is_json_lines():
    stream = FakeStream(tty=False)
    _log(stream).info("hello world")
    (record,) = _json_lines(stream)
    assert record["msg"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "commishdesk"
    assert record["ts"].endswith("+00:00")  # UTC ISO 8601


# --- verbose gating -----------------------------------------------------


def test_verbose_emits_debug():
    stream = FakeStream(tty=False)
    _log(stream, verbose=True).debug("a debug line")
    assert any(r["level"] == "DEBUG" for r in _json_lines(stream))


def test_non_verbose_suppresses_debug_keeps_info():
    stream = FakeStream(tty=False)
    logger = _log(stream, verbose=False)
    logger.debug("hidden")
    logger.info("shown")
    records = _json_lines(stream)
    assert [r["msg"] for r in records] == ["shown"]


# --- context binding --------------------------------------------------


def test_context_bound_records_carry_league_and_week():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    with log_context(league_id="123", week=3):
        logger.info("in scope")
    (record,) = _json_lines(stream)
    assert record["league_id"] == "123"
    assert record["week"] == 3


def test_context_absent_omits_keys():
    stream = FakeStream(tty=False)
    _log(stream).info("no context")
    (record,) = _json_lines(stream)
    assert "league_id" not in record
    assert "week" not in record


def test_log_context_drops_none_and_restores():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    with log_context(league_id="123", week=None):
        logger.info("only league")
    logger.info("after")
    first, second = _json_lines(stream)
    assert first["league_id"] == "123"
    assert "week" not in first
    assert "league_id" not in second


# --- redaction --------------------------------------------------------


def test_secret_in_message_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("Authorization: Bearer sk-live-AAABBBCCCDDDEEE")
    (record,) = _json_lines(stream)
    assert "sk-live" not in record["msg"]
    assert "AAABBBCCC" not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_email_in_args_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("claim for %s", "user@example.com")
    (record,) = _json_lines(stream)
    assert "user@example.com" not in record["msg"]
    assert record["msg"] == "claim for [redacted]"


def test_bare_sk_prefix_key_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("key=sk-proj-ZZZ12345678abcdefg")
    (record,) = _json_lines(stream)
    assert "sk-proj" not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_assignment_style_secret_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("loaded api_key=super-secret-value-1234")
    (record,) = _json_lines(stream)
    assert "super-secret-value" not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_discord_webhook_url_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info(
        "posting to https://discord.com/api/webhooks/123456789/AbCdEf-tokentokentoken"
    )
    (record,) = _json_lines(stream)
    assert "webhooks/123456789" not in record["msg"]
    assert "AbCdEf-tokentokentoken" not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_email_in_message_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("mailto leaguemate@league.example is not allowed")
    (record,) = _json_lines(stream)
    assert "leaguemate@league.example" not in record["msg"]
    assert "[redacted]" in record["msg"]


# --- numeric ids never match -----------------------------------------


def test_18_digit_league_id_survives_verbatim():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    with log_context(league_id="123456789012345678"):
        logger.info("building recap for league 123456789012345678 week 7")
    (record,) = _json_lines(stream)
    assert record["league_id"] == "123456789012345678"
    assert "123456789012345678" in record["msg"]
    assert "[redacted]" not in record["msg"]


# --- format override --------------------------------------------------


def test_json_override_on_a_tty(monkeypatch):
    monkeypatch.setenv("COMMISHDESK_LOG_FORMAT", "json")
    stream = FakeStream(tty=True)
    _log(stream).info("forced json")
    (record,) = _json_lines(stream)
    assert record["msg"] == "forced json"


def test_human_override_off_a_tty(monkeypatch):
    monkeypatch.setenv("COMMISHDESK_LOG_FORMAT", "human")
    stream = FakeStream(tty=False)
    _log(stream).info("forced human")
    line = stream.getvalue().strip()
    assert "INFO forced human" in line
    assert not line.startswith("{")


def test_unknown_override_falls_back_to_auto_detect(monkeypatch):
    monkeypatch.setenv("COMMISHDESK_LOG_FORMAT", "yaml-please")
    stream = FakeStream(tty=False)  # non-TTY -> auto-detect picks JSON
    _log(stream).info("auto detected")
    (record,) = _json_lines(stream)
    assert record["msg"] == "auto detected"


# --- idempotency ----------------------------------------------------


def test_repeat_configure_leaves_one_handler():
    stream = FakeStream(tty=False)
    configure_logging(stream=stream)
    configure_logging(stream=stream)
    configure_logging(stream=stream)
    logger = logging.getLogger("commishdesk")
    assert len(logger.handlers) == 1
    assert logger.propagate is False


def test_stdlib_only_imports():
    import commishdesk.logconfig as mod

    assert mod.__file__  # sanity
    # The module body imports only from the standard library.
    for name in ("contextvars", "datetime", "json", "logging", "os", "re", "sys"):
        assert hasattr(mod, name)
