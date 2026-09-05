"""MCP boundary tests: exactly three tools, typed parameters, YAML answers,
and every call drains pending file events before reading the graph."""
import asyncio

import pytest
import yaml

import main

pytestmark = pytest.mark.unit


class _FakeClient:
    def __init__(self):
        self.nodes = {
            "a.py": {"type": "File", "name": "a.py", "file_path": "a.py", "lines": {"start": 1, "end": 3}},
            "a.py:alpha": {"type": "Function", "name": "alpha", "file_path": "a.py", "lines": {"start": 1, "end": 3}},
        }
        self.workspace_path = "/ws"

    def get_all_metadata(self):
        return {k: dict(v) for k, v in self.nodes.items()}

    def contains(self, n):
        return n in self.nodes

    def get_callers(self, n):
        return []

    def get_callees(self, n):
        return []

    def get_dependents(self, n):
        return []

    def get_dependencies(self, n):
        return []

    def is_index_stale(self):
        return False

    def get_stats(self):
        return {"nodes": 2, "edges": "CSR compiled"}


class _FakeIndex:
    def __init__(self):
        self.client = _FakeClient()
        self.workspace_path = "/ws"
        self.drains = 0

    def drain_pending_events(self):
        self.drains += 1
        return 0

    def health_warnings(self):
        return ["fake warning"]


@pytest.fixture
def fake_index(monkeypatch):
    idx = _FakeIndex()
    monkeypatch.setattr(main, "_index", idx)
    return idx


def _tools():
    return {t.name: t for t in asyncio.run(main.mcp.list_tools())}


def test_exactly_three_structural_tools_are_exposed():
    tools = _tools()
    assert set(tools) == {"code_map", "lookup_symbol", "impact"}
    assert set(tools["code_map"].inputSchema["properties"]) == {"path", "depth"}
    assert set(tools["lookup_symbol"].inputSchema["properties"]) == {"symbol", "path"}
    assert tools["lookup_symbol"].inputSchema["required"] == ["symbol"]
    assert set(tools["impact"].inputSchema["properties"]) == {"symbol", "depth", "direction"}
    assert tools["impact"].inputSchema["properties"]["depth"]["default"] == 2
    assert tools["impact"].inputSchema["properties"]["depth"]["minimum"] == 1
    assert tools["impact"].inputSchema["properties"]["depth"]["maximum"] == 25
    assert tools["impact"].inputSchema["properties"]["direction"]["enum"] == ["callers", "callees"]
    for tool in tools.values():
        assert tool.description and len(tool.description) > 80


def test_tools_answer_yaml_and_drain_before_reading(fake_index):
    out = yaml.safe_load(main.lookup_symbol("alpha"))
    assert out["ok"] and out["symbol"]["id"] == "a.py:alpha"
    assert "fake warning" in out["meta"]["warnings"]
    assert fake_index.drains == 1

    out = yaml.safe_load(main.code_map())
    assert out["tool"] == "code_map" and out["meta"]["files"] == 1
    out = yaml.safe_load(main.impact("alpha", depth=1))
    assert out["tool"] == "impact" and out["meta"]["total"] == 0
    assert fake_index.drains == 3


def test_tools_fail_cleanly_before_index_exists(monkeypatch):
    monkeypatch.setattr(main, "_index", None)
    for call in (lambda: main.code_map(), lambda: main.lookup_symbol("x"), lambda: main.impact("x")):
        out = yaml.safe_load(call())
        assert out["ok"] is False and "not initialised" in out["error"]
        assert out["meta"]["graph_built"] is False
