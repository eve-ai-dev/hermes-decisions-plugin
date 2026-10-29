"""Rejected inputs/config/response schemas at real tool and HTTP boundaries."""
from importlib import import_module
import json
from types import SimpleNamespace

import pytest
import yaml
from conftest import ROOT
from gateway.session_context import set_session_vars
from test_journeys import ARGS, RESULT, server


@pytest.mark.parametrize("changes", [
    {"input": 1}, {"questions": []},
    {"questions": [{"type": "choice", "instructions": "choose"}]},
    {"questions": [{"type": "predicate", "instructions": 1}]},
    {"questions": [{"type": "predicate", "instructions": "test", "destination": "foreign"}]},
    {"background": "false"}, {"session_key": "foreign"},
    {"input": "λ" * 50000},
])
def test_request_validation_rejects_before_network(plugin, changes):
    with server() as (url, records, release):
        plugin.configure(url + "/ok")
        result = plugin.dispatch("decisions_submit", {**ARGS, **changes})
        assert "error" in result
        assert not records


@pytest.mark.parametrize("changes", [
    {"timeout": True}, {"timeout": 0}, {"timeout": 601}, {"timeout": float("nan")},
    {"credential_ref": "invalid reference"}, {"decision_model": ""},
    {"endpoint": "http://name:secret@127.0.0.1/decisions"},
    {"endpoint": "http://127.0.0.1/decisions?token=synthetic"},
])
def test_operator_config_is_checked_before_secret_or_http(plugin, changes):
    with server() as (url, records, release):
        endpoint = changes.get("endpoint", url + "/ok")
        other = {key: value for key, value in changes.items() if key != "endpoint"}
        plugin.configure(endpoint, **other)
        result = plugin.dispatch("decisions_submit", ARGS)
        assert "error" in result
        assert "name:secret" not in result["error"]
        assert not records


@pytest.mark.parametrize("answer", [
    {"type": "choice", "name": None, "probability": 0.5},
    {"type": "predicate", "name": "x", "probability": 0.5},
    {"type": "predicate", "probability": 0.5},
    {"type": "predicate", "name": None, "probability": True},
    {"type": "predicate", "name": None, "probability": 1.1},
])
def test_non_predicate_or_invalid_answer_fails_without_raw_body(plugin, answer):
    with server(json.dumps({"answers": [answer]}).encode()) as (url, records, release):
        plugin.configure(url + "/ok")
        result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
        assert result["state"] == "failed"
        assert "result" not in result
        assert len(records) == 1


@pytest.mark.parametrize("payload", [b'{"answers": []}', b'{"answers": [null]}',
                                      b'{"extra": NaN}', b'{"extra": 1e999}'])
def test_invalid_json_or_answer_count_is_not_success(plugin, payload):
    with server(payload) as (url, records, release):
        plugin.configure(url + "/ok")
        result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
        assert result["state"] == "failed"
        assert len(records) == 1


def test_exact_response_byte_limit_and_compression_rejection(plugin):
    payload = json.dumps(RESULT).encode()
    payload += b" " * (1048576 - len(payload))
    with server(payload) as (url, records, release):
        plugin.configure(url + "/ok")
        assert plugin.dispatch("decisions_submit", {**ARGS, "background": False})["result"] == RESULT
    with server(b"not compressed", encoding="gzip") as (url, records, release):
        plugin.configure(url + "/ok")
        result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
        assert "compressed responses" in result["error"]


def test_public_defaults_and_native_secret_declaration(plugin):
    # Settings omitted by the operator must match the documented public contract.
    module = plugin.manager._plugins["decisions"].module
    client = import_module(module.__name__ + ".client")
    settings = client.Settings.read(SimpleNamespace(get_config=lambda key, default: default))
    assert settings.endpoint == "https://api.openai.com/v1/decisions"
    assert settings.model == "gpt-6-luna"
    assert settings.credential_ref == "OPENAI_API_KEY"
    assert settings.timeout == 120
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    secret = manifest["config_schema"]["api_key"]
    assert secret["type"] == "secret" and secret["env"] == settings.credential_ref
    assert any(entry["name"] == settings.credential_ref and entry["secret"] is True
               for entry in manifest["requires_env"])
    assert manifest["config_schema"]["credential_ref"]["type"] == "str"
    declared = {key: spec["default"] for key, spec in manifest["config_schema"].items()
                if "default" in spec}
    assert client.Settings.read(SimpleNamespace(get_config=declared.get)) == settings


def test_public_secret_reference_works_through_native_dispatch(plugin):
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    with server() as (url, records, release):
        plugin.configure(url + "/ok", credential_ref="OPENAI_API_KEY")
        token = set_secret_scope({"OPENAI_API_KEY": "synthetic-public"})
        try:
            result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
            assert result["result"] == RESULT
            assert records[0][1] == "Bearer synthetic-public"
            assert records[0][2]["model"] == "gpt-6-luna"
        finally:
            reset_secret_scope(token)


def test_missing_origin_and_non_async_delivery_are_explicit(plugin):
    with server() as (url, records, release):
        plugin.configure(url + "/ok")
        set_session_vars()
        assert "captured session" in plugin.dispatch("decisions_submit", ARGS)["error"]
        assert not records
        set_session_vars(session_id="stateless-id", async_delivery=False)
        result = plugin.dispatch("decisions_submit", {**ARGS, "background": False})
        assert result["result"] == RESULT
        assert result["notification_accepted"] is False
        assert not plugin.notices
