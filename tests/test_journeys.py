"""Real native discovery/registry bridge + real deterministic loopback HTTP journeys."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
import json
import threading
import time

import pytest
from agent.secret_scope import set_secret_scope, reset_secret_scope
from gateway.session_context import set_session_vars
from hermes_constants import set_hermes_home_override, reset_hermes_home_override

QUESTION = {"type": "predicate", "instructions": "Is the input true?"}
ARGS = {"input": "synthetic input", "questions": [QUESTION]}
RESULT = {"answers": [{"type": "predicate", "name": None, "probability": 0.75}],
          "extra": {"quoted": "\"hi\"\nλ", "nested": [False, None, 2.5]}}


@contextmanager
def server(payload_override=None, encoding=None, outcomes=None):
    records = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            records.append((self.path, self.headers.get("Authorization"), body, raw))
            if self.path == "/hold":
                release.wait(10)
            if self.path == "/timeout":
                time.sleep(0.2)
            status = {"/redirect": 307, "/auth": 401}.get(self.path, 200)
            payload = b"not-json" if self.path == "/invalid" else json.dumps(RESULT).encode()
            if self.path == "/oversize":
                payload = b"x" * 1048577
            if payload_override is not None:
                payload = payload_override
            if outcomes is not None:
                status, payload = outcomes[len(records) - 1]
            self.send_response(status)
            self.send_header("Location", "/unexpected-target")
            self.send_header("Content-Length", str(len(payload)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = False
    thread = threading.Thread(target=httpd.serve_forever, name="test-decisions-http")
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", records, release
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(3)
        assert not thread.is_alive()


def wait_status(plugin, job_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = plugin.dispatch("decisions_status", {"job_id": job_id})
        if status.get("state") != "running":
            return status
        time.sleep(0.005)
    pytest.fail("background request did not finish after tool return")


def wait_requests(records, count):
    deadline = time.monotonic() + 2
    while len(records) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(records) == count


@pytest.mark.parametrize("running_loop", [False, True])
def test_post_return_liveness_owned_result_origin_and_scoped_credentials(plugin, running_loop):
    with server() as (url, records, release):
        plugin.configure(url + "/hold")
        if running_loop:
            async def dispatch():
                return plugin.dispatch("decisions_submit", ARGS)
            submitted = asyncio.run(dispatch())
        else:
            submitted = plugin.dispatch("decisions_submit", ARGS)
        job_id = submitted["job_id"]
        assert submitted["state"] == "running"
        wait_requests(records, 1)
        assert plugin.dispatch("decisions_status", {"job_id": job_id})["state"] == "running"
        # Second submission carries different scope, while first remains held.
        token = set_secret_scope({"TEST_DECISIONS_KEY": "synthetic-beta"})
        set_session_vars(session_key="origin-beta", session_id="id-beta")
        second = plugin.dispatch("decisions_submit", ARGS)["job_id"]
        wait_requests(records, 2)
        assert "error" in plugin.dispatch("decisions_status", {"job_id": job_id})
        release.set()
        assert wait_status(plugin, second)["result"] == RESULT
        set_session_vars(session_key="origin-alpha", session_id="id-alpha")
        status = wait_status(plugin, job_id)
        assert status["result"] == RESULT
        assert status["notification_accepted"] is True
        set_session_vars(session_id="origin-alpha")
        assert "error" in plugin.dispatch("decisions_status", {"job_id": job_id})
        set_session_vars(session_key="origin-alpha", session_id="id-alpha")
        assert {notice["session_key"] for notice in plugin.notices} == {"origin-alpha", "origin-beta"}
        assert {record[1] for record in records} == {"Bearer synthetic-alpha", "Bearer synthetic-beta"}
        assert records[0][2] == {"model": "gpt-6-luna", **ARGS}
        home = set_hermes_home_override(str(plugin.manager.home_path / "foreign"))
        try:
            assert "error" in plugin.dispatch("decisions_status", {"job_id": job_id})
        finally:
            reset_hermes_home_override(home)
            reset_secret_scope(token)


def test_foreground_uses_same_client_and_preserves_decoded_json(plugin):
    with server() as (url, records, release):
        plugin.configure(url + "/ok", decision_model="operator-model")
        result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
        assert result["state"] == "completed"
        assert result["result"] == RESULT
        assert records[0][2]["model"] == "operator-model"


@pytest.mark.parametrize("path, error", [
    ("/auth", "HTTP 401"), ("/redirect", "HTTP 307"),
    ("/timeout", "timeout"), ("/invalid", "invalid JSON"), ("/oversize", "exceeds 1048576"),
])
def test_http_errors_are_safe_and_never_retried_or_redirected(plugin, path, error):
    with server() as (url, records, release):
        plugin.configure(url + path, timeout=0.05 if path == "/timeout" else 2)
        result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
        assert result["state"] == "failed"
        assert error in result["error"]
        assert "synthetic-alpha" not in json.dumps(result)
        assert len(records) == 1
        assert records[0][0] == path


def test_destination_binding_and_missing_scoped_secret_prevent_http(plugin):
    with server() as (url, records, release):
        plugin.configure(url + "/ok", credential_endpoint=url + "/different")
        assert "exactly match" in plugin.dispatch("decisions_submit", ARGS)["error"]
        assert not records
        plugin.configure(url + "/ok")
        token = set_secret_scope({})
        try:
            result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
            assert "credential missing" in result["error"]
            assert not records
        finally:
            reset_secret_scope(token)


def test_capacity_and_unload_cancel_real_pending_http(plugin, monkeypatch):
    groups = []
    module = import_module(plugin.manager._plugins["decisions"].module.__name__ + ".repetitions")
    original_run = module.Repetitions.run

    async def observe_group(group, *args):
        groups.append(group.result)  # Observe volatile cancellation accounting before unload drops jobs.
        return await original_run(group, *args)

    monkeypatch.setattr(module.Repetitions, "run", observe_group)
    with server() as (url, records, release):
        plugin.configure(url + "/hold")
        ids = [plugin.dispatch("decisions_submit", {**ARGS, "repetitions": 3})["job_id"] for _ in range(4)]
        wait_requests(records, 4)
        assert "capacity 4" in plugin.dispatch("decisions_submit", ARGS)["error"]
        assert all(plugin.dispatch("decisions_status", {"job_id": job})["state"] == "running" for job in ids)
        started = time.monotonic()
        plugin.manager.unload()
        assert time.monotonic() - started < 2
        assert not any(t.name == "decisions-worker" and t.is_alive() for t in threading.enumerate())
        assert len(records) == 4
        assert len(groups) == 4
        for group in groups:
            assert group["requested"] == 3 and group["not_started"] == 2
            assert group["completed"] == group["success"] == group["failed"] == 0
            assert group["attempts"][0]["state"] == "cancelled"
            assert "outcome uncertain" in group["attempts"][0]["error"]


@pytest.mark.parametrize("consent", [True, False])
def test_notification_denial_does_not_lose_result(plugin, consent):
    with server() as (url, records, release):
        plugin.configure(url + "/ok", injection=consent)
        if consent:
            plugin.manager.set_gateway_message_injector(plugin, lambda **kwargs: False)
        job = plugin.dispatch("decisions_submit", ARGS)["job_id"]
        result = wait_status(plugin, job)
        assert result["result"] == RESULT
        assert result["notification_accepted"] is False
        assert not plugin.notices


def test_slow_first_completion_retained_after_32_newer_completions(plugin):
    with server() as (url, records, release):
        plugin.configure(url + "/hold", timeout=10)
        slow = plugin.dispatch("decisions_submit", ARGS)["job_id"]
        wait_requests(records, 1)
        plugin.configure(url + "/ok")
        newer = [plugin.dispatch("decisions_submit", {**ARGS, "background": False})["job_id"]
                 for _ in range(32)]
        assert plugin.dispatch("decisions_status", {"job_id": slow})["state"] == "running"
        release.set()
        assert wait_status(plugin, slow)["result"] == RESULT
        assert "error" in plugin.dispatch("decisions_status", {"job_id": newer[0]})
        assert plugin.dispatch("decisions_status", {"job_id": newer[1]})["result"] == RESULT
        assert len(records) == 33


def test_retention_is_bounded_and_status_is_explicit_on_eviction(plugin):
    with server() as (url, records, release):
        plugin.configure(url + "/ok")
        ids = [plugin.dispatch("decisions_submit", {**ARGS, "background": False})["job_id"]
               for _ in range(34)]
        assert "error" in plugin.dispatch("decisions_status", {"job_id": ids[0]})
        assert plugin.dispatch("decisions_status", {"job_id": ids[-32]})["result"] == RESULT
        assert len(records) == 34
