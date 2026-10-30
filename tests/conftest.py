"""Isolation is installed before Hermes imports; fixture loads only this candidate."""
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ["HERMES_HOME"]).resolve()
assert HOME == Path(os.environ["HOME"]).resolve()
SCRATCH = Path(os.environ["DECISIONS_TEST_SCRATCH"]).resolve()
assert HOME == SCRATCH / "home", "use an explicitly allocated disposable test home"


def audit(event, args):
    if event == "sqlite3.connect":
        raise AssertionError("tests prohibit all SQLite opens")
    if event in {"socket.connect", "socket.bind"}:
        address = args[1]
        assert isinstance(address, tuple) and address[0] == "127.0.0.1", address
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes)) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            assert Path(os.fsdecode(path)).resolve().is_relative_to(HOME.parent), path


sys.addaudithook(audit)
from hermes_cli.plugins_discovery import suppress_plugin_discovery
with suppress_plugin_discovery():
    import model_tools
from hermes_cli.plugins import PluginManager
from tools.registry import registry
from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_context, reset_multiplex_context
from gateway.session_context import set_session_vars, clear_session_vars
import pytest
import yaml


@pytest.fixture
def plugin(monkeypatch):
    home = HOME
    destination = home / "plugins/decisions"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.yaml", "__init__.py", "client.py", "worker.py", "inputs.py", "repetitions.py"):
        shutil.copyfile(ROOT / name, destination / name)
    # Installed entrypoints are not this test's source; never enumerate/import them.
    monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])
    manager = PluginManager()
    notices = []
    manager.set_gateway_message_injector(notices, lambda **kwargs: notices.append(kwargs) or True)
    scope = set_multiplex_context(True)
    secret = set_secret_scope({"TEST_DECISIONS_KEY": "synthetic-alpha"}, profile_home=str(home))
    set_session_vars(session_key="origin-alpha", session_id="id-alpha")
    harness = Harness(manager, notices)
    harness.configure("http://127.0.0.1:1/not-used")
    try:
        manager.discover_and_load()
        loaded = manager._plugins["decisions"]
        assert not loaded.error, loaded.error
        assert registry.get_entry("decisions_submit", scope=manager.scope_key)
        yield harness
    finally:
        manager.unload()
        clear_session_vars([])
        reset_secret_scope(secret)
        reset_multiplex_context(scope)
        if model_tools._tool_loop is not None and not model_tools._tool_loop.is_closed():
            model_tools._tool_loop.close()
            model_tools._tool_loop = None
        assert not list(home.rglob("*.db"))


class Harness:
    def __init__(self, manager, notices):
        self.manager = manager
        self.notices = notices

    def configure(self, endpoint, *, injection=True, **settings):
        config = {"plugins": {"enabled": ["decisions"], "entries": {"decisions": {
            "allow_gateway_injection": injection,
            "settings": {"endpoint": endpoint, "credential_endpoint": endpoint,
                         "credential_ref": "TEST_DECISIONS_KEY", "timeout": 2, **settings}}}}}
        (HOME / "config.yaml").write_text(yaml.safe_dump(config))

    def dispatch(self, name, arguments):
        import json
        return json.loads(registry.dispatch(name, arguments, scope=self.manager.scope_key))
