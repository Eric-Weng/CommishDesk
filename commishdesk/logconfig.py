"""Structured logging for the engine — JSON lines off a TTY, human lines on one.

Standard-library ``logging`` only. ``configure_logging()`` installs exactly one
handler on the ``commishdesk`` logger; ``log_context()`` binds ``league_id`` /
``week`` onto every record emitted in its scope; a redaction filter keeps secrets
and email addresses out of the output — message, ``%``-args, and exception text
alike. A bare numeric league id or week never matches a redaction pattern.
"""

from __future__ import annotations

import contextvars
import datetime
import json
import logging
import os
import re
import sys
from typing import Any, Optional

__all__ = [
    "configure_logging",
    "log_context",
    "JsonFormatter",
    "HumanFormatter",
    "ContextFilter",
    "RedactionFilter",
]

LOGGER_NAME = "commishdesk"
_FORMAT_ENV = "COMMISHDESK_LOG_FORMAT"

# --- context binding --------------------------------------------------------

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "commishdesk_log_context", default={}
)

# Keys copied from the context binding onto each record, in display order.
_CONTEXT_KEYS = ("league_id", "week")


class log_context:
    """Context manager: bind ``**fields`` onto every in-scope log record.

    ``None`` values are dropped. Nesting merges over the current binding and the
    previous binding is restored on exit. A single instance is not reentrant.
    """

    def __init__(self, **fields: Any) -> None:
        self._fields = {k: v for k, v in fields.items() if v is not None}
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> "log_context":
        if self._token is not None:
            raise RuntimeError("log_context instances are not reentrant")
        merged = dict(_log_context.get())
        merged.update(self._fields)
        self._token = _log_context.set(merged)
        return self

    def __exit__(self, *exc: object) -> bool:
        assert self._token is not None
        _log_context.reset(self._token)
        self._token = None
        return False


# --- redaction -------------------------------------------------------------

# Each pattern targets a secret- or email-shaped substring. None of them can
# match a bare run of digits, so a Sleeper league id or an NFL week is safe.
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # email address
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # Authorization: Bearer <token>
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]+"),
    # KEY=value / TOKEN: value assignment — with an optional NAMESPACE_ prefix
    # (LLM_API_KEY, X_API_TOKEN, DISCORD_WEBHOOK, MY_SECRET, ...); handles a
    # quoted value that contains spaces and stops at a comma / closing brace.
    re.compile(
        r"(?i)\b\w*(?:api[_-]?key|api[_-]?token|secret|password|token|webhook)\b"
        r"[\"']?\s*[=:]\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s\"',}]+)"
    ),
    # OpenAI / Anthropic-style secret key
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9\-]{6,}"),
    # AWS access key id
    re.compile(r"\bAKIA[A-Z0-9]{12,}"),
    # Discord webhook URL
    re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+"),
)

_REDACTED = "[redacted]"


def _redact(text: str) -> str:
    for pattern in _REDACTION_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """Render ``%``-args into the message, redact it, and clear the args.

    The formatter never sees an un-redacted value: after this filter runs the
    record carries only the cleaned string and no args. A broken format string
    must never crash logging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string / missing key must not crash logging
            message = str(record.msg)
        record.msg = _redact(message)
        record.args = ()
        return True


class ContextFilter(logging.Filter):
    """Copy the current ``log_context`` binding onto the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _log_context.get()
        for key in _CONTEXT_KEYS:
            if key in ctx:
                setattr(record, key, ctx[key])
        return True


# --- formatters -----------------------------------------------------------


def _utc_dt(created: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(created, tz=datetime.timezone.utc)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``msg`` + context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_dt(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _CONTEXT_KEYS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc_info"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """``HH:MM:SS LEVEL msg [league_id=… week=…]`` — suffix only when bound."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = _utc_dt(record.created).strftime("%H:%M:%S")
        message = record.getMessage().replace("\n", "\\n").replace("\r", "\\r")
        line = f"{stamp} {record.levelname} {message}"
        bits = [
            f"{key}={getattr(record, key)}"
            for key in _CONTEXT_KEYS
            if hasattr(record, key)
        ]
        if bits:
            line += f" [{' '.join(bits)}]"
        if record.exc_info:
            exc_text = _redact(self.formatException(record.exc_info))
            line += "\n" + exc_text
        return line


# --- setup --------------------------------------------------------------


def _use_json(stream: Any) -> bool:
    """JSON off a TTY, human on one; ``COMMISHDESK_LOG_FORMAT`` overrides.

    An unknown env value falls back to auto-detection.
    """
    override = os.environ.get(_FORMAT_ENV, "").strip().lower()
    if override == "json":
        return True
    if override == "human":
        return False
    isatty = getattr(stream, "isatty", None)
    try:
        tty = bool(isatty()) if callable(isatty) else False
    except (ValueError, OSError):
        tty = False
    return not tty


def configure_logging(verbose: bool = False, *, stream: Any = None) -> logging.Logger:
    """Configure the ``commishdesk`` logger and return it. Idempotent.

    Installs exactly one ``StreamHandler`` (replacing any prior handler), with the
    redaction and context filters, ``propagate=False``, and level
    ``DEBUG`` when *verbose* else ``INFO``. Output goes to *stream* or
    ``sys.stderr``.
    """
    target = stream if stream is not None else sys.stderr
    logger = logging.getLogger(LOGGER_NAME)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(target)
    handler.setFormatter(JsonFormatter() if _use_json(target) else HumanFormatter())
    handler.addFilter(RedactionFilter())
    handler.addFilter(ContextFilter())

    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


# Engine code may call ``logging.getLogger("commishdesk").<level>(...)`` before
# ``configure_logging()`` runs (or in a context that never calls it, e.g. a
# library import). A NullHandler keeps that from falling through to logging's
# ``lastResort`` handler, which would write unformatted, un-redacted text to
# stderr. ``configure_logging()`` clears this before adding its own handler.
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())
