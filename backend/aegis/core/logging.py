"""Structured (JSON) logging shared by every service."""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(service: str, level: str = "INFO", json_logs: bool = True) -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level.upper(), stream=sys.stdout, format="%(message)s")
    for noisy in ("httpx", "httpcore", "ccxt", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
