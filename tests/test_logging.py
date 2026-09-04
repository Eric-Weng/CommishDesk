"""Story 1.3: structured logging with redaction — one test per I/O & Edge-Case row,
plus the hardening cases from the review."""

from __future__ import annotations

import ast
import io
import json
import logging
import sys
from pathlib import Path

import pytest

from commishdesk.logconfig import (
    configure_logging,
    log_context,
)

LOGCONFIG_SRC = Path(__file__).resolve().parent.parent / "commishdesk" / "logconfig.py"


class FakeStream(io.StringIO):
    """An in-memory stream with a controllable ``isatty()``."""

    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def _clean_logging_env(monkeypatch):
    """No ambient ``COMMISHDESK_LOG_FORMAT`` unless a test sets one; fully reset the
    ``commishdesk`` logger afterwards (handlers, level, and ``propagate``)."""
    monkeypatch.delenv("COMMISHDESK_LOG_FORMAT", raising=False)
    yield
    logger = logging.getLogger("commishdesk")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    logger.addHandler(logging.NullHandler())  # restore the import-time guard


def _log(stream, *, verbose=False):
    return configure_logging(verbose=verbose, stream=stream)


def _json_lines(stream: FakeStream) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# --- TTY vs non-TTY output ------------------------------------------------


def test_tty_output_is_human_readable():
    stream = FakeStream(tty=True)
    _log(stream).info("hello world")
    line = stream.getvalue().strip()
    assert "INFO hello world" in line
    assert line[2] == ":" and line[5] == ":"  # HH:MM:SS
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
    assert [r["msg"] for r in _json_lines(stream)] == ["shown"]


def test_reconfigure_lowers_level_back_to_info():
    stream = FakeStream(tty=False)
    configure_logging(verbose=True, stream=stream)
    logger = configure_logging(verbose=False, stream=stream)
    assert logger.level == logging.INFO
    logger.debug("still hidden")
    assert _json_lines(stream) == []


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


def test_log_context_nesting_merges_and_restores_outer():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    with log_context(league_id="123"):
        with log_context(week=5):
            logger.info("inner")
        logger.info("outer again")
    logger.info("unbound")
    inner, outer, unbound = _json_lines(stream)
    assert inner["league_id"] == "123" and inner["week"] == 5
    assert outer["league_id"] == "123" and "week" not in outer
    assert "league_id" not in unbound


def test_log_context_is_not_reentrant():
    ctx = log_context(league_id="123")
    with ctx:
        with pytest.raises(RuntimeError):
            with ctx:
                pass


def test_human_formatter_context_suffix_present_and_absent():
    stream = FakeStream(tty=True)
    logger = _log(stream)
    with log_context(league_id="123", week=3):
        logger.info("bound line")
    logger.info("unbound line")
    bound, unbound = stream.getvalue().splitlines()
    assert bound.endswith(" [league_id=123 week=3]")
    assert "[league_id=" not in unbound


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


def test_email_in_message_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("mailto leaguemate@league.example is not allowed")
    (record,) = _json_lines(stream)
    assert "leaguemate@league.example" not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_bare_sk_prefix_key_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("key is sk-proj-ZZZ12345678abcdefg here")
    (record,) = _json_lines(stream)
    assert "sk-proj" not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_assignment_style_secret_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("loaded api_key=super-secret-value-1234")
    (record,) = _json_lines(stream)
    assert "super-secret-value" not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_namespaced_env_var_assignment_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("read LLM_API_KEY=hunter2plaintext from env")
    (record,) = _json_lines(stream)
    assert "hunter2plaintext" not in record["msg"]
    assert record["msg"] == "read [redacted] from env"


def test_quoted_value_with_spaces_is_fully_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info('config api_key = "long secret here" loaded')
    (record,) = _json_lines(stream)
    assert "long secret here" not in record["msg"]
    assert '"long' not in record["msg"]
    assert "[redacted]" in record["msg"]


def test_dict_literal_shaped_secret_is_redacted():
    stream = FakeStream(tty=False)
    _log(stream).info("payload {'token': 'abc123def'}")
    (record,) = _json_lines(stream)
    assert "abc123def" not in record["msg"]
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


def test_exc_info_is_redacted_in_json():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    try:
        raise ValueError("token=leaked-secret-abc123 in the exception")
    except ValueError:
        logger.exception("boom")
    (record,) = _json_lines(stream)
    assert "leaked-secret-abc123" not in record["exc_info"]
    assert "[redacted]" in record["exc_info"]


def test_exc_info_is_redacted_in_human():
    stream = FakeStream(tty=True)
    logger = _log(stream)
    try:
        raise ValueError("contact ops@example.com about this")
    except ValueError:
        logger.exception("boom")
    out = stream.getvalue()
    assert "ops@example.com" not in out
    assert "[redacted]" in out


# --- no-crash contract ----------------------------------------------


def test_bad_mapping_format_string_does_not_crash():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    logger.info("%(missing)s", {})  # KeyError inside getMessage()
    logger.info("survived")
    msgs = [r["msg"] for r in _json_lines(stream)]
    assert "survived" in msgs


def test_bad_positional_format_string_does_not_crash():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    logger.info("%s %s", "only-one")  # IndexError inside getMessage()
    logger.info("survived")
    assert "survived" in [r["msg"] for r in _json_lines(stream)]


def test_non_serializable_context_value_degrades_not_crashes():
    stream = FakeStream(tty=False)
    logger = _log(stream)
    with log_context(league_id=object()):
        logger.info("weird context")
    (record,) = _json_lines(stream)
    assert record["msg"] == "weird context"
    assert isinstance(record["league_id"], str)


# --- robustness: log-line injection --------------------------------


def test_human_formatter_neutralizes_newlines():
    stream = FakeStream(tty=True)
    _log(stream).info("line one\nINFO forged line two\r more")
    out = stream.getvalue()
    assert out.count("\n") == 1  # only the trailing newline
    assert "\\n" in out and "\\r" in out


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


# --- stdlib-only -------------------------------------------------


def test_logconfig_imports_only_stdlib():
    tree = ast.parse(LOGCONFIG_SRC.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    assert roots, "expected to find imports"
    assert roots <= set(sys.stdlib_module_names), roots
