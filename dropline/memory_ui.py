"""Local web page for viewing and editing the active session's long-term memory.

The server is a thin JSON layer over :class:`MemoryManager`, served with the standard
library's ``http.server`` on the loopback interface. A random token generated at start-up
is injected into the page and must accompany every non-GET request, and the ``Origin`` and
``Host`` headers of every request must point at this server, so a page from any other
origin cannot read or change the memory files.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import socket
import socketserver
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import Settings, current_character_name, load_settings
from .documents import list_json_files, load_json_file
from .memory import MEMORY_FILE_KEYS, MemoryManager
from .utils import now_in_timezone

LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_HEADER = "X-Dropline-Token"
TOKEN_PLACEHOLDER = "__DROPLINE_TOKEN__"
PAGE_RESOURCE = ("static", "memory_ui.html")
PAGE_PATHS = frozenset({"/", "/index.html"})
PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
EPISODE_DAYS = 7
MAX_BODY_BYTES = 256 * 1024
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "::1", "localhost"})
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})
NO_PROFILE_MESSAGE = "No active profile selected. Use `python3 -m dropline select ...` first."


class MemoryUIError(Exception):
    """An HTTP error the API reports to the page as ``{"error": ...}``."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class MemoryUIService:
    """What the page can do, independent of HTTP. Calls are serialized with a lock."""

    def __init__(self, settings: Settings) -> None:
        if settings.active_profile is None:
            raise ValueError(NO_PROFILE_MESSAGE)
        self.settings = settings
        self.profile = settings.active_profile
        self.manager = MemoryManager(settings)
        self._lock = threading.Lock()

    def _now(self) -> datetime:
        return now_in_timezone(self.settings.project.timezone)

    def overview(self) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            documents = self.settings.documents
            return {
                "session": {
                    "character_name": current_character_name(self.settings),
                    "relationship_name": self.profile.relationship.name,
                    "character_slug": self.profile.character.slug,
                    "relationship_slug": self.profile.relationship.slug,
                },
                "language": self.settings.llm.language,
                "timezone": self.settings.project.timezone,
                "generated_at": now.isoformat(),
                "episode_days": EPISODE_DAYS,
                "memory": {file_key: self.manager.list_entries(file_key) for file_key in MEMORY_FILE_KEYS},
                "conflicts": self.manager.list_conflicts(),
                "suppressed": self.manager.list_suppressed(),
                "recent_user_episodes": _recent_episode_records(documents.user_episode_dir, days=EPISODE_DAYS, now=now),
                "recent_character_episodes": _recent_episode_records(
                    documents.character_episode_dir, days=EPISODE_DAYS, now=now
                ),
            }

    def add_entry(self, file_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        content = _required_text(payload, "content")
        why_it_matters = _optional_text(payload, "why_it_matters") or ""
        with self._lock:
            existing_keys = {str(item.get("key", "")) for item in self.manager.list_entries(file_key)}
            entry = self.manager.add_manual_entry(file_key, content=content, why_it_matters=why_it_matters, now=self._now())
        return {"entry": entry, "created": entry["key"] not in existing_keys}

    def update_entry(self, file_key: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        content = _optional_text(payload, "content")
        why_it_matters = _optional_text(payload, "why_it_matters")
        if content is None and why_it_matters is None:
            raise MemoryUIError(HTTPStatus.BAD_REQUEST, "Provide content and/or why_it_matters to update.")
        with self._lock:
            entry = self.manager.update_entry(
                file_key, key, content=content, why_it_matters=why_it_matters, now=self._now()
            )
        if entry is None:
            raise MemoryUIError(HTTPStatus.NOT_FOUND, f"Memory entry not found: {key}")
        return {"entry": entry}

    def delete_entry(self, file_key: str, key: str) -> dict[str, Any]:
        with self._lock:
            deleted = self.manager.delete_entry(file_key, key, suppress=True, now=self._now())
        if not deleted:
            raise MemoryUIError(HTTPStatus.NOT_FOUND, f"Memory entry not found: {key}")
        return {"deleted": True, "key": key}

    def unsuppress(self, key: str) -> dict[str, Any]:
        with self._lock:
            restored = self.manager.unsuppress(key)
        if not restored:
            raise MemoryUIError(HTTPStatus.NOT_FOUND, f"Deleted memory not found: {key}")
        return {"restored": True, "key": key}

    def resolve_conflict(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        choice = _required_text(payload, "choice")
        with self._lock:
            resolved = self.manager.resolve_conflict(key, choice=choice, now=self._now())
        if not resolved:
            raise MemoryUIError(HTTPStatus.NOT_FOUND, f"Conflict not found: {key}")
        return {"resolved": True, "key": key, "choice": choice}


class MemoryUIServer(ThreadingHTTPServer):
    """HTTP server for the memory page; ``port=0`` picks a free port (handy in tests)."""

    daemon_threads = True

    def __init__(self, settings: Settings, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.service = MemoryUIService(settings)
        self.token = secrets.token_urlsafe(32)
        self.page_template = load_page_template()
        self.bind_host = host
        self.allowed_hostnames = LOOPBACK_HOSTNAMES if host in LOOPBACK_HOSTNAMES else frozenset({host.lower()})
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), MemoryUIHandler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind names the server with a reverse DNS lookup of the bound host,
        # which can stall on machines without DNS; the name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.bind_host
        self.server_port = self.port

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        host = f"[{self.bind_host}]" if ":" in self.bind_host else self.bind_host
        return f"http://{host}:{self.port}/"

    def render_page(self) -> bytes:
        return self.page_template.replace(TOKEN_PLACEHOLDER, html.escape(self.token)).encode("utf-8")

    def host_allowed(self, host_header: str | None) -> bool:
        parsed = _parse_host(host_header)
        if parsed is None:
            return False
        hostname, port = parsed
        if port != self.port:
            return False
        return self.bind_host in WILDCARD_HOSTS or hostname in self.allowed_hostnames

    def origin_allowed(self, origin: str | None, host_header: str | None) -> bool:
        """True when ``origin`` is this server as reached through the (already allowed) host."""
        if not origin or not self.host_allowed(host_header):
            return False
        try:
            parsed = urlsplit(origin.strip())
            hostname, port = parsed.hostname, parsed.port
        except ValueError:
            return False
        if parsed.scheme != "http" or not hostname or parsed.path not in {"", "/"}:
            return False
        return (hostname.lower(), port if port is not None else 80) == _parse_host(host_header)


class MemoryUIHandler(BaseHTTPRequestHandler):
    server: MemoryUIServer

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)

    def _handle(self, method: str) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            if not self.server.host_allowed(self.headers.get("Host")):
                raise MemoryUIError(HTTPStatus.FORBIDDEN, "Host header does not match this server.")
            if method == "GET" and path in PAGE_PATHS:
                self._send(HTTPStatus.OK, self.server.render_page(), "text/html; charset=utf-8", csp=PAGE_CSP)
                return
            status, payload = self._route(method, path)
        except MemoryUIError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except ValueError as exc:
            # MemoryManager validation: unknown memory file, empty content, bad conflict choice, ...
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except OSError:
            LOGGER.exception("Memory UI could not read or write the session's memory files")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Could not read or write the session's memory files; see the log."},
            )
        else:
            self._send_json(status, payload)

    def _route(self, method: str, path: str) -> tuple[HTTPStatus, dict[str, Any]]:
        segments = [segment for segment in path.split("/") if segment]
        service = self.server.service
        if method == "GET":
            if segments == ["api", "overview"]:
                return HTTPStatus.OK, service.overview()
            raise MemoryUIError(HTTPStatus.NOT_FOUND, f"Not found: {path}")
        self._authorize()
        body = self._read_json_body()
        match (method, segments):
            case ("POST", ["api", "memory", file_key]):
                return HTTPStatus.OK, service.add_entry(file_key, body)
            case ("PUT", ["api", "memory", file_key, key]):
                return HTTPStatus.OK, service.update_entry(file_key, key, body)
            case ("DELETE", ["api", "memory", file_key, key]):
                return HTTPStatus.OK, service.delete_entry(file_key, key)
            case ("DELETE", ["api", "suppressed", key]):
                return HTTPStatus.OK, service.unsuppress(key)
            case ("POST", ["api", "conflicts", key]):
                return HTTPStatus.OK, service.resolve_conflict(key, body)
        raise MemoryUIError(HTTPStatus.NOT_FOUND, f"Not found: {method} {path}")

    def _authorize(self) -> None:
        if not self.server.origin_allowed(self.headers.get("Origin"), self.headers.get("Host")):
            raise MemoryUIError(HTTPStatus.FORBIDDEN, "Origin header does not match this server.")
        token = self.headers.get(TOKEN_HEADER, "")
        if not token or not secrets.compare_digest(token.encode("utf-8"), self.server.token.encode("utf-8")):
            raise MemoryUIError(HTTPStatus.FORBIDDEN, f"Missing or invalid {TOKEN_HEADER} header.")

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise MemoryUIError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length header.") from None
        if length < 0:
            raise MemoryUIError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length header.")
        if length > MAX_BODY_BYTES:
            raise MemoryUIError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"Request body exceeds {MAX_BODY_BYTES} bytes.")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MemoryUIError(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.") from None
        if isinstance(payload, dict):
            return payload
        raise MemoryUIError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, *, csp: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if csp is not None:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)


def load_page_template() -> str:
    resource = resources.files(__package__).joinpath(*PAGE_RESOURCE)
    try:
        template = resource.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"The memory UI page is missing from the installed package: {resource}") from exc
    if TOKEN_PLACEHOLDER not in template:
        raise RuntimeError(f"The memory UI page has no {TOKEN_PLACEHOLDER} placeholder: {resource}")
    return template


def memory_ui_command(
    config_path: str = "config.yaml",
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    settings = load_settings(config_path, require_active_profile=False)
    if settings.active_profile is None:
        print(NO_PROFILE_MESSAGE)
        return 1
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        server = MemoryUIServer(settings, host=host, port=port)
    except OSError as exc:
        print(f"Could not listen on {host}:{port}: {exc}. Pick another port with --port.")
        return 1
    character = current_character_name(settings)
    relationship = settings.active_profile.relationship.name
    print(f"Memory UI for {character} / {relationship}: {server.url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMemory UI stopped.")
    finally:
        server.server_close()
    return 0


def _recent_episode_records(directory: Path, *, days: int, now: datetime) -> list[dict[str, Any]]:
    """Episode entries from the last ``days`` days, newest first, with the date each was filed under.

    ``MemoryManager.recent_*_episodes`` returns only the episode payloads; the page also shows
    when each one was written and what Stage 1 observed, so the dated files are read here.
    """
    cutoff = now.date() - timedelta(days=days)
    records: list[dict[str, Any]] = []
    seen_batch_ids: set[str] = set()
    for path in reversed(list_json_files(directory)):
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        entries = load_json_file(path, default=[])
        if not isinstance(entries, list):
            continue
        for item in reversed(entries):
            if not isinstance(item, dict):
                continue
            batch_id = str(item.get("batch_id", ""))
            if batch_id and batch_id in seen_batch_ids:
                continue
            if batch_id:
                seen_batch_ids.add(batch_id)
            episode = item.get("episode", item.get("payload", {}))
            record: dict[str, Any] = {
                "date": path.stem,
                "batch_id": batch_id,
                "created_at": str(item.get("created_at", "")),
                "source_dates": [str(value) for value in item.get("source_dates", [])],
                "episode": dict(episode) if isinstance(episode, dict) else {},
            }
            for extra in ("memory_observations", "open_loops"):
                if isinstance(item.get(extra), list):
                    record[extra] = list(item[extra])
            records.append(record)
    return records


def _parse_host(value: str | None) -> tuple[str, int] | None:
    """Split a ``Host`` header into (lower-cased hostname, port), or None when it is malformed."""
    if not value:
        return None
    try:
        parsed = urlsplit(f"//{value.strip()}")
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    return hostname.lower(), port if port is not None else 80


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise MemoryUIError(HTTPStatus.BAD_REQUEST, f"'{key}' must be a non-empty string.")


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise MemoryUIError(HTTPStatus.BAD_REQUEST, f"'{key}' must be a string.")
