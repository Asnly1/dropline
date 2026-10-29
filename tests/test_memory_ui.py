from __future__ import annotations

import io
import json
import logging
import re
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError

from dropline.documents import load_entries, write_json_file
from dropline.memory import suppressed_memory_path
from dropline.memory_ui import (
    TOKEN_HEADER,
    TOKEN_PLACEHOLDER,
    MemoryUIHandler,
    MemoryUIServer,
    _reachable_host,
    load_page_template,
    memory_ui_command,
)
from dropline.models import ConflictNote, ManagedMemoryEntry
from dropline.storage import StateStore
from dropline.utils import now_in_timezone
from tests.helpers import create_test_repo, load_test_settings

PROFILE_FILE_KEY = "user_profile"


def _entry(content: str, *, key: str, status: str = "active") -> dict[str, Any]:
    return ManagedMemoryEntry(
        key=key,
        content=content,
        memory_type=PROFILE_FILE_KEY,
        confidence=0.9,
        durability="stable",
        evidence=[content],
        source_dates=["2026-03-31"],
        source_message_ids=["<msg-1@example.com>"],
        created_at="2026-03-31T20:00:00-07:00",
        updated_at="2026-03-31T20:00:00-07:00",
        why_it_matters="test",
        status=status,
    ).to_dict()


def _conflict(key: str, *, existing: str, new: str) -> dict[str, Any]:
    return ConflictNote(
        key=key,
        memory_type=PROFILE_FILE_KEY,
        existing_content=existing,
        new_content=new,
        reason="Detected negation-style conflict between existing and new long-term memory.",
        source_date="2026-04-01",
        source_message_ids=["<msg-2@example.com>"],
        created_at="2026-04-01T20:00:00-07:00",
        occurrence_dates=["2026-04-01"],
    ).to_dict()


def _decode(response: Any) -> Any:
    body = response.read().decode("utf-8")
    if response.headers.get("Content-Type", "").startswith("application/json"):
        return json.loads(body)
    return body


class MemoryUIServerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.root = Path(tmp_dir.name)
        create_test_repo(self.root)
        self.settings = load_test_settings(self.root)
        self.server = MemoryUIServer(self.settings, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = self.server.url.rstrip("/")
        # Talk to the loopback server directly even when the environment configures an HTTP proxy.
        self.opener = request.build_opener(request.ProxyHandler({}))

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        data = raw_body if raw_body is not None else (None if body is None else json.dumps(body).encode("utf-8"))
        req = request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth and method != "GET":
            req.add_header(TOKEN_HEADER, self.server.token)
            req.add_header("Origin", self.base_url)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with self.opener.open(req, timeout=5) as response:
                return response.status, _decode(response), dict(response.headers)
        except HTTPError as exc:
            return exc.code, _decode(exc), dict(exc.headers)

    def _overview(self) -> dict[str, Any]:
        status, payload, _ = self._request("GET", "/api/overview")
        self.assertEqual(status, 200)
        return payload

    def test_server_binds_to_a_free_loopback_port_and_exposes_a_token(self) -> None:
        host, port = self.server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(port, 0)
        self.assertEqual(self.server.url, f"http://127.0.0.1:{port}/")
        self.assertGreaterEqual(len(self.server.token), 32)

    def test_overview_structure(self) -> None:
        status, payload, headers = self._request("GET", "/api/overview")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(
            payload["session"],
            {
                "character_name": "刻晴",
                "relationship_name": "青梅竹马",
                "character_slug": "keqing",
                "relationship_slug": "childhood-friends",
            },
        )
        self.assertEqual(payload["language"], "简体中文")
        self.assertEqual(payload["memory"], {"user_profile": [], "relationship_memory": [], "active_arc": []})
        self.assertEqual(payload["conflicts"], [])
        self.assertEqual(payload["suppressed"], [])
        self.assertEqual(payload["recent_user_episodes"], [])
        self.assertEqual(payload["recent_character_episodes"], [])
        self.assertEqual(payload["episode_days"], 7)
        self.assertEqual(payload["timezone"], "America/Los_Angeles")

    def test_overview_lists_only_the_last_seven_days_of_episodes_with_dates(self) -> None:
        today = now_in_timezone(self.settings.project.timezone).date()
        old = today - timedelta(days=10)
        user_dir = self.settings.documents.user_episode_dir
        character_dir = self.settings.documents.character_episode_dir
        write_json_file(
            user_dir / f"{today.isoformat()}.json",
            [
                {
                    "entry_id": "episode:batch-1",
                    "batch_id": "batch-1",
                    "created_at": f"{today.isoformat()}T20:00:00-07:00",
                    "source_message_ids": ["<msg-1@example.com>"],
                    "source_dates": [today.isoformat()],
                    "episode": {"main_event": "你说自己在准备考试。", "state": "认真", "possible_needs": ["回应"]},
                    "memory_observations": [{"content": "用户在准备考试", "type": "user_profile"}],
                    "open_loops": ["考试结果"],
                }
            ],
        )
        write_json_file(
            user_dir / f"{old.isoformat()}.json",
            [{"entry_id": "episode:batch-0", "batch_id": "batch-0", "episode": {"main_event": "很久以前"}}],
        )
        write_json_file(
            character_dir / f"{today.isoformat()}.json",
            [{"entry_id": "episode:batch-1", "batch_id": "batch-1", "payload": {"summary": "回信了", "continuity_value": "低"}}],
        )

        payload = self._overview()

        self.assertEqual(len(payload["recent_user_episodes"]), 1)
        record = payload["recent_user_episodes"][0]
        self.assertEqual(record["date"], today.isoformat())
        self.assertEqual(record["batch_id"], "batch-1")
        self.assertEqual(record["source_dates"], [today.isoformat()])
        self.assertEqual(record["episode"]["main_event"], "你说自己在准备考试。")
        self.assertEqual(record["memory_observations"], [{"content": "用户在准备考试", "type": "user_profile"}])
        self.assertEqual(record["open_loops"], ["考试结果"])
        self.assertEqual(
            payload["recent_character_episodes"],
            [
                {
                    "date": today.isoformat(),
                    "batch_id": "batch-1",
                    "created_at": "",
                    "source_dates": [],
                    "episode": {"summary": "回信了", "continuity_value": "低"},
                }
            ],
        )

    def test_overview_tolerates_hand_edited_episode_and_tombstone_files(self) -> None:
        today = now_in_timezone(self.settings.project.timezone).date()
        write_json_file(
            self.settings.documents.user_episode_dir / f"{today.isoformat()}.json",
            [
                {
                    "batch_id": "batch-1",
                    "source_dates": 20260331,
                    "episode": {"main_event": "你说自己在准备考试。"},
                    "memory_observations": "用户在准备考试",
                    "open_loops": {"topic": "考试结果"},
                }
            ],
        )
        write_json_file(suppressed_memory_path(self.settings), [1, "x", {"key": "suppressed-1", "content": "用户喜欢猫"}])

        payload = self._overview()

        record = payload["recent_user_episodes"][0]
        self.assertEqual(record["source_dates"], [])
        self.assertEqual(record["episode"], {"main_event": "你说自己在准备考试。"})
        self.assertNotIn("memory_observations", record)
        self.assertNotIn("open_loops", record)
        self.assertEqual(payload["suppressed"], [{"key": "suppressed-1", "content": "用户喜欢猫"}])

    def test_malformed_session_files_are_reported_as_500_without_a_traceback(self) -> None:
        documents = self.settings.documents
        profile_path = documents.user_profile
        cases = (
            (
                "confidence is null",
                profile_path,
                [{**_entry("用户喜欢猫", key="user_profile-1"), "confidence": None}],
                "POST",
                {"content": "用户喜欢狗"},
            ),
            ("memory entries are numbers", profile_path, [1, 2], "GET", None),
            ("conflict notes are numbers", documents.conflict_notes, [1], "GET", None),
        )
        for label, file_path, entries, method, body in cases:
            with self.subTest(case=label):
                write_json_file(file_path, entries)
                path = "/api/overview" if method == "GET" else "/api/memory/user_profile"

                with self.assertLogs("dropline.memory_ui", level="ERROR") as logs:
                    status, payload, headers = self._request(method, path, body)

                self.assertEqual(status, 500)
                self.assertTrue(headers["Content-Type"].startswith("application/json"))
                self.assertEqual(payload, {"error": "Unexpected server error, possibly a malformed session file; see the log."})
                self.assertEqual(len(logs.records), 1)
                self.assertIn("Traceback", logs.output[0])
                write_json_file(file_path, [])

        for label, raw in (("not json", b"[{not json"), ("not utf-8", b"\xff\xfe[]")):
            with self.subTest(case=label):
                profile_path.write_bytes(raw)
                with self.assertLogs("dropline.memory_ui", level="ERROR"):
                    status, payload, _ = self._request("GET", "/api/overview")
                self.assertEqual(status, 500)
                self.assertEqual(payload, {"error": "One of the session's files is not valid UTF-8 JSON; see the log."})

        # The server keeps answering once the file is fixed.
        write_json_file(profile_path, [_entry("用户喜欢猫", key="user_profile-1")])
        self.assertEqual([item["key"] for item in self._overview()["memory"]["user_profile"]], ["user_profile-1"])

    def test_add_update_delete_tombstone_restore_round_trip(self) -> None:
        status, payload, _ = self._request(
            "POST",
            "/api/memory/user_profile",
            {"content": "用户养了一只叫团子的猫", "why_it_matters": "写信时可以问候"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertFalse(payload["revived"])
        entry = payload["entry"]
        key = entry["key"]
        self.assertEqual(entry["content"], "用户养了一只叫团子的猫")
        self.assertEqual(entry["why_it_matters"], "写信时可以问候")
        self.assertEqual(entry["evidence"], ["manual entry"])
        self.assertEqual(entry["status"], "active")
        self.assertEqual([item["key"] for item in self._overview()["memory"]["user_profile"]], [key])

        status, payload, _ = self._request("POST", "/api/memory/user_profile", {"content": "用户养了一只叫团子的猫"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["created"])
        self.assertFalse(payload["revived"])
        self.assertEqual(payload["entry"]["key"], key)
        self.assertEqual(len(load_entries(self.settings.documents.user_profile)), 1)

        status, payload, _ = self._request("PUT", f"/api/memory/user_profile/{key}", {"content": "用户养了一只叫团子的橘猫"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"]["content"], "用户养了一只叫团子的橘猫")
        self.assertEqual(payload["entry"]["why_it_matters"], "写信时可以问候")
        self.assertIn("manual edit", payload["entry"]["evidence"])

        status, payload, _ = self._request("PUT", f"/api/memory/user_profile/{key}", {"why_it_matters": "它叫团子"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"]["why_it_matters"], "它叫团子")
        on_disk = load_entries(self.settings.documents.user_profile)
        self.assertEqual([(item["content"], item["why_it_matters"]) for item in on_disk], [("用户养了一只叫团子的橘猫", "它叫团子")])

        status, payload, _ = self._request("DELETE", f"/api/memory/user_profile/{key}")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"deleted": True, "key": key})
        overview = self._overview()
        self.assertEqual(overview["memory"]["user_profile"], [])
        self.assertEqual(len(overview["suppressed"]), 1)
        tombstone = overview["suppressed"][0]
        self.assertEqual(tombstone["content"], "用户养了一只叫团子的橘猫")
        self.assertEqual(tombstone["reason"], "deleted from user_profile")

        status, payload, _ = self._request("DELETE", f"/api/suppressed/{tombstone['key']}")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"restored": True, "key": tombstone["key"]})
        self.assertEqual(self._overview()["suppressed"], [])

    def test_adding_a_deleted_memory_again_removes_its_tombstone(self) -> None:
        _, payload, _ = self._request("POST", "/api/memory/relationship_memory", {"content": "用户希望被称呼为小鱼"})
        key = payload["entry"]["key"]
        self._request("DELETE", f"/api/memory/relationship_memory/{key}")
        self.assertEqual(len(self._overview()["suppressed"]), 1)

        status, payload, _ = self._request("POST", "/api/memory/relationship_memory", {"content": "用户希望被称呼为小鱼"})

        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertFalse(payload["revived"])
        overview = self._overview()
        self.assertEqual(overview["suppressed"], [])
        self.assertEqual([item["content"] for item in overview["memory"]["relationship_memory"]], ["用户希望被称呼为小鱼"])

    def test_adding_the_content_of_a_superseded_entry_revives_it(self) -> None:
        write_json_file(
            self.settings.documents.user_profile,
            [
                {**_entry("用户喜欢猫", key="user_profile-old", status="superseded"), "superseded_by": "user_profile-new"},
                _entry("用户不喜欢猫", key="user_profile-new"),
            ],
        )

        status, payload, _ = self._request("POST", "/api/memory/user_profile", {"content": "用户喜欢猫"})

        self.assertEqual(status, 200)
        self.assertEqual((payload["created"], payload["revived"]), (False, True))
        self.assertEqual(payload["entry"]["key"], "user_profile-old")
        self.assertEqual(payload["entry"]["status"], "active")
        entries = self._overview()["memory"]["user_profile"]
        self.assertEqual(
            [(item["key"], item["status"], item["superseded_by"]) for item in entries],
            [("user_profile-old", "active", ""), ("user_profile-new", "superseded", "user_profile-old")],
        )

    def test_writes_are_refused_with_409_while_a_pipeline_run_holds_the_lock(self) -> None:
        cases = (
            ("POST", "/api/memory/user_profile", {"content": "用户喜欢猫"}),
            ("PUT", "/api/memory/user_profile/user_profile-1", {"content": "用户喜欢狗"}),
            ("DELETE", "/api/memory/user_profile/user_profile-1", None),
            ("DELETE", "/api/suppressed/suppressed-1", None),
            ("POST", "/api/conflicts/conflict-1", {"choice": "take_new"}),
        )
        write_json_file(self.settings.documents.user_profile, [_entry("用户喜欢猫", key="user_profile-1")])

        with StateStore(self.settings).acquire_lock():
            for method, path, body in cases:
                with self.subTest(method=method, path=path):
                    status, payload, _ = self._request(method, path, body)
                    self.assertEqual(status, 409)
                    self.assertIn("pipeline run is in progress", payload["error"])
            # Reading is not affected, and nothing was changed.
            self.assertEqual([item["content"] for item in self._overview()["memory"]["user_profile"]], ["用户喜欢猫"])

        status, payload, _ = self._request("PUT", "/api/memory/user_profile/user_profile-1", {"content": "用户喜欢狗"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"]["content"], "用户喜欢狗")
        # The page releases the run lock after each write, so the pipeline can take it again.
        with StateStore(self.settings).acquire_lock():
            pass

    def test_active_arc_limit_is_reported_as_400(self) -> None:
        for content in ("用户在准备考试", "用户在找新房子"):
            status, _, _ = self._request("POST", "/api/memory/active_arc", {"content": content})
            self.assertEqual(status, 200)

        status, payload, _ = self._request("POST", "/api/memory/active_arc", {"content": "用户在学做饭"})

        self.assertEqual(status, 400)
        self.assertIn("active_arc holds at most 2 entries", payload["error"])

    def test_resolve_conflict_take_new_supersedes_existing_entry(self) -> None:
        write_json_file(self.settings.documents.user_profile, [_entry("用户喜欢猫", key="user_profile-old")])
        write_json_file(self.settings.documents.conflict_notes, [_conflict("conflict-1", existing="用户喜欢猫", new="用户不喜欢猫")])
        self.assertEqual(len(self._overview()["conflicts"]), 1)

        status, payload, _ = self._request("POST", "/api/conflicts/conflict-1", {"choice": "take_new"})

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"resolved": True, "key": "conflict-1", "choice": "take_new"})
        overview = self._overview()
        self.assertEqual(overview["conflicts"], [])
        entries = overview["memory"]["user_profile"]
        self.assertEqual([(item["content"], item["status"]) for item in entries], [("用户喜欢猫", "superseded"), ("用户不喜欢猫", "active")])
        self.assertEqual(entries[0]["superseded_by"], entries[1]["key"])

    def test_resolve_conflict_keep_existing_only_drops_the_note(self) -> None:
        write_json_file(self.settings.documents.user_profile, [_entry("用户喜欢猫", key="user_profile-old")])
        write_json_file(self.settings.documents.conflict_notes, [_conflict("conflict-1", existing="用户喜欢猫", new="用户不喜欢猫")])

        status, payload, _ = self._request("POST", "/api/conflicts/conflict-1", {"choice": "keep_existing"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["choice"], "keep_existing")
        overview = self._overview()
        self.assertEqual(overview["conflicts"], [])
        self.assertEqual([(item["content"], item["status"]) for item in overview["memory"]["user_profile"]], [("用户喜欢猫", "active")])

    def test_resolve_conflict_rejects_bad_choice_and_unknown_key(self) -> None:
        write_json_file(self.settings.documents.conflict_notes, [_conflict("conflict-1", existing="用户喜欢猫", new="用户不喜欢猫")])

        status, payload, _ = self._request("POST", "/api/conflicts/conflict-1", {"choice": "flip a coin"})
        self.assertEqual(status, 400)
        self.assertIn("keep_existing", payload["error"])

        status, payload, _ = self._request("POST", "/api/conflicts/conflict-1", {})
        self.assertEqual(status, 400)
        self.assertIn("choice", payload["error"])

        status, payload, _ = self._request("POST", "/api/conflicts/missing", {"choice": "take_new"})
        self.assertEqual(status, 404)
        self.assertIn("missing", payload["error"])
        self.assertEqual(len(self._overview()["conflicts"]), 1)

    def test_non_get_requests_without_token_are_forbidden(self) -> None:
        cases = (
            ("no token", {"Origin": self.base_url}),
            ("wrong token", {"Origin": self.base_url, TOKEN_HEADER: "not-the-token"}),
            ("empty token", {"Origin": self.base_url, TOKEN_HEADER: ""}),
        )
        for label, headers in cases:
            with self.subTest(case=label):
                status, payload, _ = self._request(
                    "POST", "/api/memory/user_profile", {"content": "用户喜欢猫"}, auth=False, headers=headers
                )
                self.assertEqual(status, 403)
                self.assertIn(TOKEN_HEADER, payload["error"])
        self.assertEqual(self._overview()["memory"]["user_profile"], [])

    def test_non_get_requests_with_wrong_origin_are_forbidden(self) -> None:
        port = self.server.server_address[1]
        cases = (
            ("missing origin", {TOKEN_HEADER: self.server.token}),
            ("other site", {TOKEN_HEADER: self.server.token, "Origin": "http://evil.example"}),
            ("other port", {TOKEN_HEADER: self.server.token, "Origin": f"http://127.0.0.1:{port + 1}"}),
            ("null origin", {TOKEN_HEADER: self.server.token, "Origin": "null"}),
            ("https", {TOKEN_HEADER: self.server.token, "Origin": f"https://127.0.0.1:{port}"}),
        )
        for label, headers in cases:
            with self.subTest(case=label):
                status, payload, _ = self._request(
                    "DELETE", "/api/suppressed/anything", auth=False, headers=headers
                )
                self.assertEqual(status, 403)
                self.assertIn("Origin", payload["error"])

    def test_localhost_origin_is_accepted_for_the_loopback_server(self) -> None:
        port = self.server.server_address[1]
        headers = {TOKEN_HEADER: self.server.token, "Origin": f"http://localhost:{port}", "Host": f"localhost:{port}"}

        status, payload, _ = self._request(
            "POST", "/api/memory/user_profile", {"content": "用户喜欢猫"}, auth=False, headers=headers
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])

    def test_requests_for_another_host_are_forbidden(self) -> None:
        port = self.server.server_address[1]
        for method, path in (("GET", "/api/overview"), ("GET", "/"), ("POST", "/api/memory/user_profile")):
            with self.subTest(method=method, path=path):
                status, payload, _ = self._request(
                    method, path, {"content": "x"} if method == "POST" else None, headers={"Host": f"evil.example:{port}"}
                )
                self.assertEqual(status, 403)
                self.assertIn("Host", payload["error"])

    def test_unknown_memory_file_is_400(self) -> None:
        status, payload, _ = self._request("POST", "/api/memory/secrets", {"content": "用户喜欢猫"})

        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "Unknown memory file: secrets"})
        status, payload, _ = self._request("DELETE", "/api/memory/secrets/user_profile-1")
        self.assertEqual(status, 400)
        self.assertIn("Unknown memory file", payload["error"])

    def test_missing_keys_are_404(self) -> None:
        cases = (
            ("PUT", "/api/memory/user_profile/user_profile-missing", {"content": "x"}),
            ("DELETE", "/api/memory/user_profile/user_profile-missing", None),
            ("DELETE", "/api/suppressed/suppressed-missing", None),
        )
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                status, payload, _ = self._request(method, path, body)
                self.assertEqual(status, 404)
                self.assertIn("missing", payload["error"])

    def test_invalid_bodies_are_400(self) -> None:
        cases = (
            ("empty content", "POST", "/api/memory/user_profile", {"content": "   "}, None),
            ("content not a string", "POST", "/api/memory/user_profile", {"content": 42}, None),
            ("why not a string", "POST", "/api/memory/user_profile", {"content": "x", "why_it_matters": ["y"]}, None),
            ("no fields", "PUT", "/api/memory/user_profile/user_profile-1", {}, None),
            ("not json", "POST", "/api/memory/user_profile", None, b"{not json"),
            ("not an object", "POST", "/api/memory/user_profile", None, b"[1, 2]"),
        )
        for label, method, path, body, raw in cases:
            with self.subTest(case=label):
                status, payload, _ = self._request(method, path, body, raw_body=raw)
                self.assertEqual(status, 400)
                self.assertTrue(payload["error"])
        self.assertEqual(self._overview()["memory"]["user_profile"], [])

    def test_unknown_routes_are_404(self) -> None:
        cases = (
            ("GET", "/nope"),
            ("GET", "/api/memory/user_profile"),
            ("POST", "/api/memory"),
            ("PUT", "/api/suppressed/key"),
        )
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, payload, _ = self._request(method, path, {} if method != "GET" else None)
                self.assertEqual(status, 404)
                self.assertIn("Not found", payload["error"])

    def test_page_is_served_with_the_token_injected(self) -> None:
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, body, headers = self._request("GET", path)

                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertIn(f'<meta name="dropline-token" content="{self.server.token}">', body)
                self.assertNotIn(TOKEN_PLACEHOLDER, body)
                self.assertNotIn("http://", body.split("<body>", 1)[1].split("<script>", 1)[0])

    def test_handler_times_out_idle_connections_and_ignores_a_vanished_client(self) -> None:
        self.assertEqual(MemoryUIHandler.timeout, 30)
        for error in (BrokenPipeError("Broken pipe"), ConnectionResetError("Connection reset by peer")):
            with self.subTest(error=type(error).__name__):
                handler = MemoryUIHandler.__new__(MemoryUIHandler)
                handler.client_address = ("127.0.0.1", 54321)
                handler.request_version = "HTTP/1.1"
                handler.requestline = "GET /api/overview HTTP/1.1"
                handler.command = "GET"
                handler.close_connection = True
                handler.wfile = _VanishedClient(error)

                with self.assertLogs("dropline.memory_ui", level="DEBUG") as logs:
                    handler._send_json(HTTPStatus.OK, {"ok": True})

                self.assertTrue(handler.wfile.writes)
                self.assertTrue(any("connection closed before the response was sent" in line for line in logs.output))
                self.assertFalse(any(record.levelno > logging.DEBUG for record in logs.records))


class _VanishedClient:
    """A ``wfile`` whose peer has gone away: every write fails with the given error."""

    def __init__(self, error: OSError) -> None:
        self.error = error
        self.writes = 0

    def write(self, data: bytes) -> int:
        self.writes += 1
        raise self.error

    def flush(self) -> None:
        pass


class WildcardBindTests(unittest.TestCase):
    """A server bound to every interface still only answers requests addressed by IP or ``localhost``."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.root = Path(tmp_dir.name)
        create_test_repo(self.root)
        self.settings = load_test_settings(self.root)
        self.server = MemoryUIServer(self.settings, host="0.0.0.0", port=0)
        self.port = self.server.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.opener = request.build_opener(request.ProxyHandler({}))

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str, host_header: str) -> tuple[int, Any]:
        req = request.Request(f"http://127.0.0.1:{self.port}{path}", method="GET")
        req.add_header("Host", host_header)
        try:
            with self.opener.open(req, timeout=5) as response:
                return response.status, _decode(response)
        except HTTPError as exc:
            return exc.code, _decode(exc)

    def test_url_points_at_the_loopback_address(self) -> None:
        self.assertEqual(self.server.url, f"http://127.0.0.1:{self.port}/")
        self.assertEqual(_reachable_host("::"), "::1")
        self.assertEqual(_reachable_host(""), "127.0.0.1")
        self.assertEqual(_reachable_host("192.0.2.10"), "192.0.2.10")

    def test_ip_literals_and_localhost_are_accepted(self) -> None:
        for host_header in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}", f"192.0.2.10:{self.port}"):
            with self.subTest(host=host_header):
                status, payload = self._get("/api/overview", host_header)
                self.assertEqual(status, 200)
                self.assertIn("memory", payload)

    def test_domain_names_are_rejected_to_block_dns_rebinding(self) -> None:
        cases = (
            f"evil.example:{self.port}",
            f"127.0.0.1.evil.example:{self.port}",
            f"app.localhost:{self.port}",
            f"127.0.0.1:{self.port + 1}",
        )
        for host_header in cases:
            with self.subTest(host=host_header):
                for path in ("/", "/api/overview"):
                    status, payload = self._get(path, host_header)
                    self.assertEqual(status, 403)
                    self.assertIn("Host", payload["error"])

    def test_ipv6_wildcard_url_uses_the_ipv6_loopback(self) -> None:
        try:
            server = MemoryUIServer(self.settings, host="::", port=0)
        except OSError as exc:
            self.skipTest(f"IPv6 is not available here: {exc}")
        self.addCleanup(server.server_close)
        self.assertEqual(server.url, f"http://[::1]:{server.port}/")


class MemoryUIPageTests(unittest.TestCase):
    """The page is plain JavaScript without a test runner, so its guards are pinned by inspecting the source."""

    def setUp(self) -> None:
        self.template = load_page_template()
        self.script = self.template.split("<script>", 1)[1].split("</script>", 1)[0]

    def _function_body(self, name: str) -> str:
        match = re.search(rf"\n  function {name}\([^)]*\) \{{\n(.*?)\n  \}}\n", self.script, re.DOTALL)
        self.assertIsNotNone(match, f"function {name}() not found in the page")
        return match.group(1)

    def test_evidence_labels_only_use_own_keys(self) -> None:
        self.assertIn("Object.prototype.hasOwnProperty.call(EVIDENCE_KEYS, item)", self._function_body("evidenceLabel"))

    def test_revived_entries_get_their_own_message(self) -> None:
        self.assertEqual(self.script.count("msg_revived:"), 2)
        self.assertIn("已恢复为生效", self.script)
        self.assertIn("result.revived", self._function_body("addedMessageKey"))
        self.assertIn("addedMessageKey(result)", self.script)

    def test_toolbar_buttons_are_bound_once_outside_init(self) -> None:
        init_body = self._function_body("init")
        self.assertNotIn("addEventListener", init_body)
        self.assertIn("onclick: init", init_body)
        self.assertEqual(self.script.count('addEventListener("click"'), 2)
        bind_body = self._function_body("bindControls")
        self.assertIn('getElementById("refresh-button").addEventListener("click"', bind_body)
        self.assertIn('getElementById("lang-button").addEventListener("click"', bind_body)
        self.assertEqual(self.script.count("bindControls();"), 1)
        self.assertLess(self.script.index("bindControls();"), self.script.rindex("init();"))


class MemoryUICommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.root = Path(tmp_dir.name)
        create_test_repo(self.root)
        self.config_path = str(self.root / "config.yaml")

    def test_returns_1_without_an_active_profile(self) -> None:
        (self.root / "profiles" / "active.json").unlink()
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = memory_ui_command(self.config_path, port=0, open_browser=False)

        self.assertEqual(exit_code, 1)
        self.assertIn("No active profile selected", stdout.getvalue())

    def test_prints_the_address_opens_the_browser_and_stops_on_ctrl_c(self) -> None:
        stdout = io.StringIO()
        with (
            patch("dropline.memory_ui.webbrowser.open") as open_browser,
            patch.object(MemoryUIServer, "serve_forever", side_effect=KeyboardInterrupt),
            redirect_stdout(stdout),
            self.assertNoLogs("dropline.memory_ui", level="WARNING"),
        ):
            exit_code = memory_ui_command(self.config_path, host="127.0.0.1", port=0, open_browser=True)

        self.assertEqual(exit_code, 0)
        open_browser.assert_called_once()
        url = open_browser.call_args.args[0]
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn(f"Memory UI for 刻晴 / 青梅竹马: {url}", stdout.getvalue())
        self.assertIn("Memory UI stopped.", stdout.getvalue())

    def test_non_loopback_bind_warns_and_opens_the_page_through_loopback(self) -> None:
        stdout = io.StringIO()
        with (
            patch("dropline.memory_ui.webbrowser.open") as open_browser,
            patch.object(MemoryUIServer, "serve_forever", side_effect=KeyboardInterrupt),
            redirect_stdout(stdout),
            self.assertLogs("dropline.memory_ui", level="WARNING") as logs,
        ):
            exit_code = memory_ui_command(self.config_path, host="0.0.0.0", port=0, open_browser=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        self.assertIn("Listening on 0.0.0.0, which is not a loopback address", logs.output[0])
        self.assertIn("SSH tunnel", logs.output[0])
        url = open_browser.call_args.args[0]
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn(f"Memory UI for 刻晴 / 青梅竹马: {url}", stdout.getvalue())
        self.assertNotIn("0.0.0.0", stdout.getvalue())

    def test_no_browser_skips_opening_the_page(self) -> None:
        with (
            patch("dropline.memory_ui.webbrowser.open") as open_browser,
            patch.object(MemoryUIServer, "serve_forever", side_effect=KeyboardInterrupt),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = memory_ui_command(self.config_path, port=0, open_browser=False)

        self.assertEqual(exit_code, 0)
        open_browser.assert_not_called()

    def test_port_in_use_returns_1_with_a_hint(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        stdout = io.StringIO()

        with patch("dropline.memory_ui.webbrowser.open") as open_browser, redirect_stdout(stdout):
            exit_code = memory_ui_command(self.config_path, host="127.0.0.1", port=port, open_browser=True)

        self.assertEqual(exit_code, 1)
        open_browser.assert_not_called()
        self.assertIn(f"Could not listen on 127.0.0.1:{port}", stdout.getvalue())
        self.assertIn("--port", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
