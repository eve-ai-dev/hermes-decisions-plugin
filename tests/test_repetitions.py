"""Changed contracts at native dispatch/HTTP boundaries; reuse the existing harness."""
from importlib import import_module
import asyncio
import json
import os

import pytest
from test_journeys import ARGS, QUESTION, server, wait_requests, wait_status


@pytest.mark.parametrize("changes, message", [
    ({"repetitions": True}, "integer"), ({"repetitions": 1.5}, "integer"),
    ({"repetitions": 0}, "1..20"), ({"repetitions": 21}, "1..20"),
    ({"input_path": "/not-read"}, "exactly one"),
])
def test_new_argument_rejections_before_http(plugin, changes, message):
    with server() as (url, records, release):
        plugin.configure(url + "/ok")
        result = plugin.dispatch("decisions_submit", {**ARGS, **changes})
        assert message in result["error"]
        assert not records


@pytest.mark.parametrize("case, message", [
    ("disabled", "disabled"), ("escape", "outside"), ("symlink", "outside"),
    ("fifo", "regular"), ("utf8", "UTF-8"), ("size", "262144"),
    ("escaped-size", "request exceeds"),
])
def test_file_boundary_rejects_without_http(plugin, tmp_path, case, message):
    root = tmp_path / "inputs"
    root.mkdir(exist_ok=True)
    path = root / case
    outside = tmp_path / "outside.txt"
    outside.write_text("private synthetic content")
    if case == "symlink":
        path.symlink_to(outside)
    elif case == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes({"utf8": b"\xff", "size": b"x" * 262145,
                          "escaped-size": ("λ" * 50000).encode()}.get(case, b"input"))
    if case == "escape":
        path = root / ".." / outside.name
    with server() as (url, records, release):
        plugin.configure(url + "/ok", input_roots=[] if case == "disabled" else [str(root)])
        result = plugin.dispatch("decisions_submit", {"input_path": str(path), "questions": [QUESTION]})
        assert message in result["error"]
        assert not records


def test_file_is_frozen_once_with_settings_and_exact_request_bytes(plugin, tmp_path, monkeypatch):
    root = tmp_path / "frozen-input"
    root.mkdir()
    path = root / "input.txt"
    path.write_text("original UTF-8 λ\n")
    inputs = import_module(plugin.manager._plugins["decisions"].module.__name__ + ".inputs")
    original_read = inputs.read_input
    reads = []

    def counted_read(value, ctx):
        reads.append(value)
        return original_read(value, ctx)

    monkeypatch.setattr(inputs, "read_input", counted_read)
    with server() as (url, records, release):
        plugin.configure(url + "/hold", input_roots=[str(root)], decision_model="frozen-model")
        job = plugin.dispatch("decisions_submit", {
            "input_path": str(path), "questions": [QUESTION], "repetitions": 20})["job_id"]
        wait_requests(records, 1)
        path.write_text("changed input")
        plugin.configure(url + "/auth", decision_model="changed-model")
        release.set()
        status = wait_status(plugin, job)
        assert status["state"] == "completed"
        assert status["result"]["success"] == status["result"]["requested"] == 20
        assert reads == [str(path)]
        assert len(records) == 20
        assert {record[3] for record in records} == {records[0][3]}
        assert all(record[0] == "/hold" for record in records)
        assert records[0][2] == {"model": "frozen-model", "input": "original UTF-8 λ\n",
                                  "questions": [QUESTION]}


@pytest.mark.parametrize("codes, state, values", [
    ([200, 200, 200], "completed", [0.2, 0.5, 0.8]),
    ([200, 401, 200], "partial", [0.2, 0.8]),
    ([401, 401, 401], "failed", []),
])
def test_group_summary_and_failures_through_foreground_http(plugin, codes, state, values):
    outcomes = [(code, json.dumps({"answers": [
        {"type": "predicate", "name": None, "probability": value},
        {"type": "predicate", "name": None, "probability": 1 - value},
    ]}).encode()) for code, value in zip(codes, [0.2, 0.5, 0.8])]
    with server(outcomes=outcomes) as (url, records, release):
        plugin.configure(url + "/ok")
        status = plugin.dispatch("decisions_submit", {
            **ARGS, "questions": [QUESTION, {**QUESTION, "instructions": "Is the opposite true?"}],
            "repetitions": 3, "background": False})
        assert status["state"] == state
        result = status["result"]
        assert {key: result[key] for key in ("requested", "completed", "success", "failed", "not_started")} == {
            "requested": 3, "completed": 3, "success": len(values), "failed": 3 - len(values), "not_started": 0}
        assert [attempt["state"] for attempt in result["attempts"]] == [
            "completed" if code == 200 else "failed" for code in codes]
        for attempt in result["attempts"]:
            if attempt["state"] == "failed":
                assert "HTTP 401" in attempt["error"]
        for question, expected in zip(result["questions"], [values, [1 - v for v in values]]):
            assert question["probabilities"] == expected
            assert question["mean"] == (pytest.approx(sum(expected) / len(expected)) if expected else None)
            assert question["min"] == (min(expected) if expected else None)
            assert question["max"] == (max(expected) if expected else None)
        assert len(records) == 3
        assert all(set(record[2]) == {"model", "input", "questions"} for record in records)


def test_foreground_budget_returns_handle_through_running_loop_bridge(plugin, monkeypatch):
    worker = import_module(plugin.manager._plugins["decisions"].module.__name__ + ".worker")
    monkeypatch.setattr(worker, "FOREGROUND_WAIT_LIMIT", 0.05)
    with server() as (url, records, release):
        plugin.configure(url + "/hold")

        async def dispatch():
            return plugin.dispatch("decisions_submit", {**ARGS, "repetitions": 3, "background": False})

        result = asyncio.run(dispatch())
        assert "foreground wait timeout" in result["error"]
        job = result["job_id"]
        wait_requests(records, 1)
        progress = plugin.dispatch("decisions_status", {"job_id": job})
        assert progress["state"] == "running"
        assert progress["result"]["completed"] == 0
        assert progress["result"]["not_started"] == 2
        assert progress["result"]["attempts"] == [{"state": "running"}]
        release.set()
        final = wait_status(plugin, job)
        assert final["state"] == "completed" and final["result"]["completed"] == 3
        assert progress["result"]["completed"] == 0  # Returned snapshot is detached from progress.


def test_unexpected_internal_error_stops_group_with_completed_counts(plugin, monkeypatch):
    module = import_module(plugin.manager._plugins["decisions"].module.__name__ + ".repetitions")
    original_decide = module.decide
    calls = []

    async def fail_second(settings, body, count):
        calls.append(body)
        if len(calls) == 2:
            raise RuntimeError("private synthetic diagnostic")
        return await original_decide(settings, body, count)

    monkeypatch.setattr(module, "decide", fail_second)
    with server() as (url, records, release):
        plugin.configure(url + "/ok")
        status = plugin.dispatch("decisions_submit", {**ARGS, "repetitions": 3, "background": False})
        assert status["state"] == "failed"
        result = status["result"]
        assert {key: result[key] for key in ("requested", "completed", "success", "failed", "not_started")} == {
            "requested": 3, "completed": 2, "success": 1, "failed": 1, "not_started": 1}
        assert len(calls) == 2 and len(records) == 1
        assert result["questions"][0]["probabilities"] == [0.75]
        assert result["attempts"][1] == {
            "state": "failed", "error": "internal Decisions error; outcome uncertain, not retried"}
        assert "private synthetic diagnostic" not in json.dumps(status)
