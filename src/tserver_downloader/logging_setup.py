"""Logging setup: rotating file handler + console, with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

LOGGER_NAME = "tserver"

# Header/value names whose values must never appear in logs.
_SENSITIVE_KEYS = (
    "authorization", "cookie", "set-cookie", "token", "password",
    "secret", "api-key", "apikey", "x-api-key", "session", "auth",
)

# Matches  key = value  /  key: value  /  "key": "value"  pairs.
_PAIR_RE = re.compile(
    r"(?i)(authorization|cookie|set-cookie|token|password|secret|api[-_]?key"
    r"|x-api-key|session|auth)\s*([=:])\s*\"?[^\"\s,;}\]]+\"?"
)
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-~+/=]+")


def redact_secrets(text: str) -> str:
    """Redact secret values from log text.

    Replaces the value after a sensitive key (``Authorization: ...``,
    ``token=...``, ``"password": "..."``) and Bearer tokens with
    ``[REDACTED]``. Safe to call on any string; non-strings are stringified.
    """
    if not isinstance(text, str):
        text = str(text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    return _PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)} [REDACTED]", text)


class _RedactingFormatter(logging.Formatter):
    """Formatter that redacts secrets from the rendered message."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_secrets(rendered)


def setup_logging(log_dir: str | Path, level: int = logging.INFO) -> logging.Logger:
    """Configure the ``tserver`` logger.

    Writes rotating logs (1 MB x 5 backups) to ``<log_dir>/tserver.log``
    plus a console handler. Idempotent: repeated calls do not duplicate
    handlers. Returns the logger.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = _RedactingFormatter(
        fmt='{"time": "%(asctime)s", "level": "%(levelname)s", '
            '"name": "%(name)s", "msg": "%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "tserver.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def get_logger() -> logging.Logger:
    """Return the ``tserver`` logger (unconfigured if setup not called)."""
    return logging.getLogger(LOGGER_NAME)
