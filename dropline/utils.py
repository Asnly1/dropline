from __future__ import annotations

import hashlib
import imaplib
import logging
import smtplib
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo

try:
    import httpx
except ImportError:  # pragma: no cover - optional dependency at import time
    httpx = None


TRANSIENT_ERROR_FRAGMENTS = (
    "server disconnected without sending a response",
    "incomplete chunked read",
    "peer closed connection",
    "temporarily unavailable",
    "temporary failure in name resolution",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "nodename nor servname provided, or not known",
    "timed out",
)
T = TypeVar("T")


def now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    return f"run-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def hash_text(value: str, prefix: str = "") -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}{digest}" if prefix else digest


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def resolve_path_within_roots(path_value: str | Path, allowed_roots: Iterable[str | Path]) -> Path:
    target = Path(path_value).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if any(target == root or target.is_relative_to(root) for root in roots):
        return target
    raise RuntimeError(f"Refusing to delete path outside managed roots: {target}")


def is_transient_network_error(exc: BaseException) -> bool:
    if isinstance(exc, smtplib.SMTPException) and not isinstance(
        exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)
    ):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, imaplib.IMAP4.abort)):
        return True
    if httpx is not None and isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.TransportError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    message = str(exc).lower()
    return any(fragment in message for fragment in TRANSIENT_ERROR_FRAGMENTS)


def retry_call(
    operation: Callable[[], T],
    *,
    operation_name: str,
    logger: logging.Logger,
    attempts: int = 3,
    initial_delay_seconds: float = 1.0,
    backoff_multiplier: float = 2.0,
    should_retry: Callable[[BaseException], bool] = is_transient_network_error,
) -> T:
    delay_seconds = max(initial_delay_seconds, 0.0)
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001
            if attempt >= attempts or not should_retry(exc):
                raise
            logger.warning(
                "%s failed on attempt %s/%s: %s. Retrying in %.1fs.",
                operation_name,
                attempt,
                attempts,
                exc,
                delay_seconds,
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            delay_seconds *= backoff_multiplier
