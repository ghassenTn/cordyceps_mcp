"""Unit tests for the structural tools (code_map / lookup_symbol / impact).

These run against an in-memory fake client so they test the resolution,
projection and traversal rules in isolation from the Rust engine. End-to-end
behaviour on real parsed workspaces is covered by ``test_indexing.py``.
"""
import pytest

from src.structure import CodeStructure, IndexView, resolve_symbol, RESOLVED, AMBIGUOUS, NOT_FOUND, normalize_path
from src.structure.service import MAX_IMPACT_NODES, MAX_RELATED

pytestmark = pytest.mark.unit


class FakeClient:
    """Minimal EngramClient look-alike: metadata + typed edge sets."""

    def __init__(self, workspace_path="/ws"):
        self.workspace_path = workspace_path
        self.nodes = {}
        self.calls = []        # executable (from, to)
        self.structural = []   # structural (from, to)
        self.stale = False
        self.built = True

    def add(self, node_id, ntype, name=None, file_path=None, **meta):
        if file_path is None:
            file_path = node_id if ntype in ("File", "Folder") else node_id.split(":", 1)[0]
        if name is None:
            if ntype in ("File", "Folder"):
                name = node_id.rsplit("/", 1)[-1]
            else:
                name = node_id.split(":", 1)[1].rsplit(".", 1)[-1]
        self.nodes[node_id] = {"type": ntype, "name": name, "file_path": file_path, **meta}
        return self

    def call(self, a, b):
        self.calls.append((a, b))
        return self

    def contains_edge(self, a, b):
        self.structural.append((a, b))
        return self

    # -- client API used by src.structure --
    def get_all_metadata(self):
        return {k: dict(v) for k, v in self.nodes.items()}

    def contains(self, node_id):
        return node_id in self.nodes

    def get_callers(self, n):
        return [a for a, b in self.calls if b == n]

    def get_callees(self, n):
        return [b for a, b in self.calls if a == n]

    def get_dependents(self, n):
        return self.get_callers(n) + [a for a, b in self.structural if b == n]

    def get_dependencies(self, n):
        return self.get_callees(n) + [b for a, b in self.structural if a == n]

    def is_index_stale(self):
        return self.stale

    def get_stats(self):
        return {"nodes": len(self.nodes), "edges": "CSR compiled" if self.built else "not built"}


@pytest.fixture
def client():
    c = FakeClient()
    c.add("src", "Folder")
    c.add("src/api.py", "File", lines={"start": 1, "end": 80}, imports=["os", "src.services"])
    c.add("src/api.py:SalesAPI", "Class", lines={"start": 5, "end": 40}, signature="class SalesAPI(View)",
          base_classes=["View"], decorators=["api_view"], body="SECRET BODY")
    c.add("src/api.py:SalesAPI.create_sale", "Function", lines={"start": 10, "end": 20},
          signature="def create_sale(self, request)", docstring="Create a sale.", param_count=2,
          calls=["create_sale", "json.dumps"], body="SECRET BODY")
    c.add("src/api.py:create_sale", "Function", lines={"start": 50, "end": 60}, calls=["persist"],
          signature="def create_sale(payload)")
    c.add("src/services.py", "File", lines={"start": 1, "end": 30})
    c.add("src/services.py:persist", "Function", lines={"start": 1, "end": 10}, calls=["write_db"])
    c.add("src/services.py:persist.inner", "Function", lines={"start": 3, "end": 6})
    c.add("src/db.py", "File", lines={"start": 1, "end": 12})
    c.add("src/db.py:write_db", "Function", lines={"start": 1, "end": 12})
    c.add("web/App.tsx", "File", lines={"start": 1, "end": 40}, imports=["react"])
    c.add("web/App.tsx:App", "Function", lines={"start": 1, "end": 40})
    c.add("README.md", "File", lines={"start": 1, "end": 5})
    # containment
    for child, parent in [("src/api.py", "src"), ("src/services.py", "src"), ("src/db.py", "src"),
                          ("src/api.py:SalesAPI", "src/api.py"), ("src/api.py:SalesAPI.create_sale", "src/api.py:SalesAPI"),
                          ("src/api.py:create_sale", "src/api.py"), ("src/services.py:persist", "src/services.py"),
                          ("src/services.py:persist.inner", "src/services.py:persist"),
                          ("src/services.py:persist.inner", "src/services.py"),
                          ("src/db.py:write_db", "src/db.py"), ("web/App.tsx:App", "web/App.tsx")]:
        c.contains_edge(child, parent)
    # imports (imported -> importer)
    c.contains_edge("src/services.py", "src/api.py")
    # calls
    c.call("src/api.py:SalesAPI.create_sale", "src/api.py:create_sale")
    c.call("src/api.py:create_sale", "src/services.py:persist")
    c.call("src/services.py:persist", "src/db.py:write_db")
    c.call("web/App.tsx:App", "src/api.py:SalesAPI.create_sale")
    return c


@pytest.fixture
def structure(client):
    return CodeStructure(client, "/ws")


# ── path normalisation ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", ""), (".", ""), ("./", ""), ("src/", "src"), ("./src/api.py", "src/api.py"),
    ("src\\api.py", "src/api.py"), ("/ws/src", "src"), ("/ws", ""), ("/elsewhere/x.py", "/elsewhere/x.py"),
])
def test_normalize_path(raw, expected):
    assert normalize_path(raw, "/ws") == expected


# ── resolver ──────────────────────────────────────────────────────────

class TestResolver:
    def test_exact_id_wins(self, client):
        view = IndexView(client)
        res = resolve_symbol(view, "src/api.py:create_sale")
        assert res.status == RESOLVED and res.node_id == "src/api.py:create_sale"

    def test_bare_name_with_duplicates_is_ambiguous_not_first_match(self, client):
        res = resolve_symbol(IndexView(client), "create_sale")
        assert res.status == AMBIGUOUS
        assert res.candidates == ["src/api.py:SalesAPI.create_sale", "src/api.py:create_sale"]

    def test_qualified_name_disambiguates(self, client):
        res = resolve_symbol(IndexView(client), "SalesAPI.create_sale")
        assert res.status == RESOLVED and res.node_id == "src/api.py:SalesAPI.create_sale"

    def test_file_shorthand_and_suffix(self, client):
        view = IndexView(client)
        assert resolve_symbol(view, "services.py:persist").node_id == "src/services.py:persist"
        # nested definitions resolve through their qualified suffix
        assert resolve_symbol(view, "services.py:inner").node_id == "src/services.py:persist.inner"
        assert resolve_symbol(view, "persist.inner").node_id == "src/services.py:persist.inner"

    def test_path_scope_filters_candidates(self, client):
        view = IndexView(client)
        res = resolve_symbol(view, "App", path="web")
        assert res.status == RESOLVED and res.node_id == "web/App.tsx:App"
        assert resolve_symbol(view, "App", path="src").status == NOT_FOUND

    def test_case_insensitive_fallback(self, client):
        res = resolve_symbol(IndexView(client), "WRITE_DB")
        assert res.status == RESOLVED and res.node_id == "src/db.py:write_db"

    def test_file_and_folder_by_path(self, client):
        view = IndexView(client)
        assert resolve_symbol(view, "api.py").node_id == "src/api.py"
        assert resolve_symbol(view, "src/api.py").node_id == "src/api.py"
        assert resolve_symbol(view, "src").node_id == "src"

    def test_not_found_gives_suggestions(self, client):
        res = resolve_symbol(IndexView(client), "creat_sale")
        assert res.status == NOT_FOUND
        assert "src/api.py:create_sale" in res.suggestions or "src/api.py:SalesAPI.create_sale" in res.suggestions

    def test_absolute_workspace_path_is_accepted(self, client):
        res = resolve_symbol(IndexView(client), "/ws/src/api.py:create_sale", workspace_path="/ws")
        assert res.node_id == "src/api.py:create_sale"


# ── lookup_symbol ─────────────────────────────────────────────────────

class TestLookup:
    def test_resolved_record_is_allowlisted(self, structure):
        out = structure.lookup("SalesAPI.create_sale")
        assert out["ok"] and out["status"] == "resolved"
        sym = out["symbol"]
        assert sym["id"] == "src/api.py:SalesAPI.create_sale"
        assert sym["kind"] == "Function" and sym["lines"] == "10-20"
        assert sym["container"] == "src/api.py:SalesAPI"
        assert sym["signature"].startswith("def create_sale")
        assert "body" not in sym and "extra_json" not in sym and "calls" not in sym
        assert "SECRET" not in str(out)

    def test_relationships_separate_executable_from_structural(self, structure):
        rel = structure.lookup("SalesAPI.create_sale")["relationships"]
        assert rel["callers"] == ["web/App.tsx:App"]
        assert rel["callees"] == ["src/api.py:create_sale"]
        # json.dumps has no node; create_sale resolved -> only the former is unresolved
        assert rel["unresolved_calls"] == ["json.dumps"]
        assert "related" not in rel  # containment is not reported as a relationship

    def test_members_listed_for_containers(self, structure):
        rel = structure.lookup("persist", path="src/services.py")["relationships"]
        assert rel["members"] == ["src/services.py:persist.inner"]
        cls = structure.lookup("SalesAPI")
        assert cls["symbol"]["base_classes"] == ["View"]
        assert cls["relationships"]["members"] == ["src/api.py:SalesAPI.create_sale"]

    def test_ambiguous_returns_candidates(self, structure):
        out = structure.lookup("create_sale")
        assert out["ok"] and out["status"] == "ambiguous"
        assert [c["id"] for c in out["candidates"]] == ["src/api.py:SalesAPI.create_sale", "src/api.py:create_sale"]
        assert out["meta"]["candidates"] == 2

    def test_not_found_is_explicit(self, structure):
        out = structure.lookup("does_not_exist_anywhere")
        assert out["ok"] is False and out["status"] == "not_found"
        assert "error" in out

    def test_file_lookup_reports_imports_and_definitions(self, structure):
        out = structure.lookup("api.py")
        assert out["symbol"]["kind"] == "File" and out["symbol"]["line_count"] == 80
        rel = out["relationships"]
        assert rel["imports"] == ["src/services.py"]
        assert rel["defines"] == ["SalesAPI [Class] L5-40", "create_sale [Function] L50-60"]
        assert structure.lookup("services.py")["relationships"]["imported_by"] == ["src/api.py"]

    def test_folder_lookup_points_to_code_map(self, structure):
        out = structure.lookup("src")
        assert out["symbol"]["kind"] == "Folder" and "code_map" in out["hint"]

    def test_callers_complete_and_deduplicated(self):
        c = FakeClient()
        c.add("t.py", "File").add("t.py:work", "Function")
        for i in range(MAX_RELATED + 10):
            c.add(f"c{i}.py", "File").add(f"c{i}.py:caller", "Function")
            c.call(f"c{i}.py:caller", "t.py:work")
            c.call(f"c{i}.py:caller", "t.py:work")  # duplicate call site
        rel = CodeStructure(c).lookup("work")["relationships"]
        assert len(rel["callers"]) == MAX_RELATED
        assert len(set(rel["callers"])) == MAX_RELATED
        assert rel["callers_omitted"] == 10


# ── impact ────────────────────────────────────────────────────────────

class TestImpact:
    def test_direct_vs_transitive_with_depths(self, structure):
        out = structure.impact("write_db", depth=3)
        assert out["ok"]
        m = out["meta"]
        assert m["direct"] == 1 and m["total"] == 3 and m["files"] == 2
        # Direct neighbours are the depth=1 entries; no duplicate list, no legend.
        assert "direct_callers" not in out and "entry_format" not in m
        assert out["affected"] == {
            "src/api.py": ["SalesAPI.create_sale [Function] L10-20 depth=3",
                           "create_sale [Function] L50-60 depth=2"],
            "src/services.py": ["persist [Function] L1-10 depth=1"],
        }
        # App (depth 4) is beyond the requested depth and must be flagged, not hidden.
        assert "web/App.tsx" not in out["affected"]
        assert m["more_beyond_depth"] is True

    def test_depth_one_is_direct_only(self, structure):
        out = structure.impact("write_db", depth=1)
        assert out["meta"]["total"] == out["meta"]["direct"] == 1
        assert list(out["affected"]) == ["src/services.py"]

    def test_full_depth_reaches_frontend_without_name_matching_warning(self, structure):
        out = structure.impact("write_db", depth=10)
        assert "web/App.tsx" in out["affected"]
        assert out["meta"]["more_beyond_depth"] is False
        assert not any("name" in w and "false positives" in w
                       for w in out["meta"].get("warnings", []))

    def test_structural_edges_never_inflate_impact(self, structure):
        # services.py is imported by api.py, but nothing calls the File node.
        out = structure.impact("services.py", depth=5)
        assert out["ok"] and out["meta"]["total"] == 0 and out["affected"] == {}

    def test_callees_direction(self, structure):
        out = structure.impact("SalesAPI.create_sale", depth=5, direction="callees")
        assert out["meta"]["direction"] == "callees"
        assert out["meta"]["direct"] == 1
        assert out["affected"]["src/api.py"] == ["create_sale [Function] L50-60 depth=1"]
        assert set(out["affected"]) == {"src/api.py", "src/services.py", "src/db.py"}

    def test_invalid_direction_and_depth_clamping(self, structure):
        assert structure.impact("write_db", direction="sideways")["ok"] is False
        assert structure.impact("write_db", depth=0)["meta"]["depth"] == 1
        assert structure.impact("write_db", depth=999)["meta"]["depth"] == 25

    def test_cycles_terminate(self):
        c = FakeClient()
        for n in "abc":
            c.add(f"{n}.py", "File").add(f"{n}.py:f", "Function")
        c.call("a.py:f", "b.py:f").call("b.py:f", "c.py:f").call("c.py:f", "a.py:f")
        out = CodeStructure(c).impact("a.py:f", depth=10)
        assert out["meta"]["total"] == 2 and out["meta"]["more_beyond_depth"] is False

    def test_large_fanout_is_truncated_explicitly(self):
        c = FakeClient()
        c.add("t.py", "File").add("t.py:hot", "Function")
        for i in range(MAX_IMPACT_NODES + 50):
            c.add(f"c{i}.py", "File").add(f"c{i}.py:caller", "Function").call(f"c{i}.py:caller", "t.py:hot")
        out = CodeStructure(c).impact("hot", depth=1)
        assert out["meta"]["truncated"] is True
        assert out["meta"]["total"] == MAX_IMPACT_NODES
        assert out["meta"]["direct"] == MAX_IMPACT_NODES + 50
        assert sum(len(v) for v in out["affected"].values()) == MAX_IMPACT_NODES
        assert any("stopped" in w for w in out["meta"]["warnings"])

    def test_exact_node_limit_is_not_reported_as_truncated(self):
        c = FakeClient()
        c.add("t.py", "File").add("t.py:hot", "Function")
        for i in range(MAX_IMPACT_NODES):
            c.add(f"c{i}.py", "File").add(f"c{i}.py:caller", "Function").call(
                f"c{i}.py:caller", "t.py:hot")
        out = CodeStructure(c).impact("hot", depth=1)
        assert out["meta"]["total"] == MAX_IMPACT_NODES
        assert out["meta"]["truncated"] is False

    def test_javascript_target_uses_contextual_confidence(self, client):
        out = CodeStructure(client).impact("web/App.tsx:App", direction="callees", depth=5)
        assert not any("name" in warning and "false positives" in warning
                       for warning in out["meta"].get("warnings", []))

    def test_ambiguous_target_is_not_guessed(self, structure):
        out = structure.impact("create_sale")
        assert out["status"] == "ambiguous" and "affected" not in out


# ── code_map ──────────────────────────────────────────────────────────

class TestCodeMap:
    def test_root_directory(self, structure):
        out = structure.map(".")
        assert out["ok"] and out["meta"]["kind"] == "directory"
        assert out["meta"]["files"] == 5 and out["meta"]["folders"] == 2
        assert [f["path"] for f in out["folders"]] == ["src", "web"]
        src = out["folders"][0]
        assert src["files_count"] == 3 and src["symbols_count"] == 6
        assert [f["path"] for f in out["files"]] == ["README.md"]

    def test_depth_expands_nested_folders(self, structure):
        out = structure.map(".", depth=2)
        src = out["folders"][0]
        files = {f["path"]: f for f in src["files"]}
        assert files["src/api.py"]["symbols"] == {"Class": 1, "Function": 2}
        assert files["src/api.py"]["top_level"] == "SalesAPI, create_sale"
        assert files["src/api.py"]["line_count"] == 80

    def test_file_map_is_a_definition_tree(self, structure):
        out = structure.map("src/api.py")
        assert out["meta"]["kind"] == "file" and out["meta"]["line_count"] == 80
        assert out["imports"] == ["os", "src.services"]
        names = [s["name"] for s in out["symbols"]]
        assert names == ["SalesAPI", "create_sale"]
        cls = out["symbols"][0]
        assert cls["kind"] == "Class" and cls["base_classes"] == ["View"]
        assert cls["members"][0]["name"] == "create_sale" and cls["members"][0]["lines"] == "10-20"
        assert "SECRET" not in str(out)

    def test_nested_function_appears_under_parent(self, structure):
        out = structure.map("src/services.py")
        assert out["symbols"][0]["name"] == "persist"
        assert out["symbols"][0]["members"][0]["name"] == "inner"

    def test_subfolder_and_absolute_path(self, structure):
        assert [f["path"] for f in structure.map("src")["files"]] == ["src/api.py", "src/db.py", "src/services.py"]
        assert structure.map("/ws/src")["meta"]["path"] == "src"

    def test_unknown_path_errors_with_suggestions(self, structure):
        out = structure.map("srx")
        assert out["ok"] is False and "src" in out["suggestions"]

    def test_empty_workspace_has_an_empty_root_map(self):
        out = CodeStructure(FakeClient()).map(".")
        assert out["ok"] is True
        assert out["meta"]["files"] == 0 and out["meta"]["symbols"] == 0

    def test_large_inline_metadata_is_bounded(self):
        c = FakeClient()
        values = ["x" * 500] + [f"decorator_{i}" for i in range(29)]
        frameworks = [f"framework_{i}" for i in range(30)]
        c.add("a.py", "File", frameworks=frameworks).add(
            "a.py:work", "Function", decorators=values)
        lookup = CodeStructure(c).lookup("work")["symbol"]
        assert len(lookup["decorators"]) == 20
        assert lookup["decorators_omitted"] == 10
        assert all(len(item) <= 240 for item in lookup["decorators"])
        mapped = CodeStructure(c).map("a.py")["symbols"][0]
        assert len(mapped["decorators"]) == 20
        assert mapped["decorators_omitted"] == 10
        file_record = CodeStructure(c).lookup("a.py")["symbol"]
        assert len(file_record["frameworks"]) == 20
        assert file_record["frameworks_omitted"] == 10


# ── inline callbacks are transparent ──────────────────────────────────

@pytest.fixture
def react_client():
    """Graph shape the parser produces for a React component:

        function App() {
          useEffect(() => {                       // App.callback_L5_C13
            const checkBackend = async () => { ping(); };
            const t = setInterval(() => tick());  // ...callback_L10_C7
          });
          const handleSend = useCallback((text) => {
            items.forEach((i) => send(i));        // handleSend.callback_L22_C9
          });
          return <Header />;
        }
    """
    c = FakeClient()
    c.add("web", "Folder")
    c.add("web/App.tsx", "File", lines={"start": 1, "end": 40}, imports=["react"],
          javascript_exports={"App": {"kind": "local", "local": "App"},
                              "default": {"kind": "local", "local": "App"}})
    c.add("web/App.tsx:App", "Function", lines={"start": 1, "end": 40}, signature="function App()",
          calls=["useEffect", "callback_L5_C13", "Header", "setVitals", "renderer.destroy", "process.exit"],
          javascript_shadowed_names=["vitals", "setVitals", "renderer"])
    c.add("web/App.tsx:App.callback_L5_C13", "Function", lines={"start": 5, "end": 12}, signature="() =>",
          calls=["checkBackend", "setInterval", "callback_L10_C7"])
    c.add("web/App.tsx:App.callback_L5_C13.checkBackend", "Function", lines={"start": 6, "end": 9},
          signature="checkBackend = async () =>", calls=["ping"])
    c.add("web/App.tsx:App.callback_L5_C13.callback_L10_C7", "Function", lines={"start": 10, "end": 10},
          calls=["tick"])
    c.add("web/App.tsx:App.handleSend", "Function", lines={"start": 20, "end": 30},
          signature="handleSend = useCallback((text) =>",
          calls=["items.forEach", "callback_L22_C9", "setVitals", "text.trim", "analytics.track"],
          javascript_shadowed_names=["text"])
    c.add("web/App.tsx:App.handleSend.callback_L22_C9", "Function", lines={"start": 22, "end": 22},
          calls=["send"])
    c.add("web/Header.tsx", "File").add("web/Header.tsx:Header", "Function", lines={"start": 1, "end": 5})
    c.add("web/api.ts", "File")
    for name, line in (("ping", 1), ("tick", 5), ("send", 9)):
        c.add(f"web/api.ts:{name}", "Function", lines={"start": line, "end": line + 2})
    for child in [n for n in c.nodes if ":" in n]:
        parent = f"{child.rsplit('.', 1)[0]}" if "." in child.split(":", 1)[1] else child.split(":", 1)[0]
        c.contains_edge(child, parent)
    c.call("web/App.tsx:App", "web/App.tsx:App.callback_L5_C13")
    c.call("web/App.tsx:App", "web/Header.tsx:Header")
    c.call("web/App.tsx:App.callback_L5_C13", "web/App.tsx:App.callback_L5_C13.checkBackend")
    c.call("web/App.tsx:App.callback_L5_C13", "web/App.tsx:App.callback_L5_C13.callback_L10_C7")
    c.call("web/App.tsx:App.callback_L5_C13.checkBackend", "web/api.ts:ping")
    c.call("web/App.tsx:App.callback_L5_C13.callback_L10_C7", "web/api.ts:tick")
    c.call("web/App.tsx:App.handleSend", "web/App.tsx:App.handleSend.callback_L22_C9")
    c.call("web/App.tsx:App.handleSend.callback_L22_C9", "web/api.ts:send")
    return c


class TestInlineCallbacks:
    def test_code_map_hides_callbacks_and_hoists_named_children(self, react_client):
        out = CodeStructure(react_client).map("web/App.tsx")
        assert "callback_L" not in str(out)
        assert out["meta"]["symbols"] == 3 and out["meta"]["inline_callbacks"] == 3
        app = out["symbols"][0]
        assert app["name"] == "App"
        assert [m["name"] for m in app["members"]] == ["checkBackend", "handleSend"]
        assert "members" not in app["members"][0]
        assert out["exports"] == ["App", "App as default"]

    def test_directory_counts_exclude_callbacks(self, react_client):
        out = CodeStructure(react_client).map("web")
        files = {f["path"]: f for f in out["files"]}
        assert files["web/App.tsx"]["symbols"] == {"Function": 3}
        assert files["web/App.tsx"]["top_level"] == "App"
        assert out["meta"]["symbols"] == 7

    def test_lookup_attributes_callback_calls_to_the_definition(self, react_client):
        out = CodeStructure(react_client).lookup("App")
        rel = out["relationships"]
        assert rel["callees"] == ["web/Header.tsx:Header", "web/App.tsx:App.callback_L5_C13.checkBackend",
                                  "web/api.ts:tick"]
        assert rel["members"] == ["web/App.tsx:App.callback_L5_C13.checkBackend", "web/App.tsx:App.handleSend"]
        assert "related" not in rel
        # `callback_L*` is wiring, setInterval/process are globals, setVitals and
        # renderer are local bindings (state setter / hook result); the hook remains.
        assert rel["unresolved_calls"] == ["useEffect"]

    def test_unresolved_calls_skip_bindings_of_enclosing_scopes(self, react_client):
        rel = CodeStructure(react_client).lookup("handleSend")["relationships"]
        # setVitals is bound in App (the container), text is handleSend's own parameter.
        assert rel["unresolved_calls"] == ["analytics.track"]
        assert rel["callees"] == ["web/api.ts:send"]

    def test_hoisted_definition_shows_named_container_and_display_name(self, react_client):
        out = CodeStructure(react_client).lookup("checkBackend")
        sym = out["symbol"]
        assert sym["id"] == "web/App.tsx:App.callback_L5_C13.checkBackend"
        assert sym["name"] == "App.checkBackend" and sym["container"] == "web/App.tsx:App"
        assert out["relationships"]["callers"] == ["web/App.tsx:App"]
        assert out["relationships"]["callees"] == ["web/api.ts:ping"]

    def test_display_name_resolves(self, react_client):
        view = IndexView(react_client)
        for query in ("App.checkBackend", "App.tsx:App.checkBackend", "web/App.tsx:App.checkBackend"):
            res = resolve_symbol(view, query)
            assert res.status == RESOLVED, query
            assert res.node_id == "web/App.tsx:App.callback_L5_C13.checkBackend"

    def test_impact_counts_hops_between_named_definitions_only(self, react_client):
        s = CodeStructure(react_client)
        callers = s.impact("ping", depth=1)
        assert callers["affected"] == {"web/App.tsx": ["App.checkBackend [Function] L6-9 depth=1"]}
        assert callers["meta"]["more_beyond_depth"] is True
        assert s.impact("ping", depth=2)["affected"]["web/App.tsx"] == [
            "App [Function] L1-40 depth=2", "App.checkBackend [Function] L6-9 depth=1"]
        callees = s.impact("App", depth=1, direction="callees")
        assert callees["meta"]["direct"] == 3 and callees["meta"]["total"] == 3
        assert callees["affected"] == {
            "web/App.tsx": ["App.checkBackend [Function] L6-9 depth=1"],
            "web/Header.tsx": ["Header [Function] L1-5 depth=1"],
            "web/api.ts": ["tick [Function] L5-7 depth=1"],
        }
        # handleSend is defined, not called, by App: its callee is not App's.
        assert "send" not in str(callees["affected"])
        assert s.impact("send", depth=1)["affected"] == {"web/App.tsx": ["App.handleSend [Function] L20-30 depth=1"]}


def test_export_entries_render_compactly():
    from src.structure.records import export_entries
    js = {"javascript_exports": {
        "c": {"kind": "reexport", "source": "./x", "imported": "c"},
        "b": {"kind": "reexport", "source": "./x", "imported": "a"},
        "*": [{"kind": "star", "source": "./y"}],
        "bee": {"kind": "local", "local": "b"},
        "default": {"kind": "local", "local": "anonymous_L1"},
    }}
    assert export_entries(js) == ["c from ./x", "a as b from ./x", "* from ./y", "b as bee", "default"]
    generic = {"exports": [{"default": False, "names": [{"name": "a", "alias": None}, {"name": "b", "alias": "bee"}],
                            "source": None},
                           {"default": True, "names": [{"name": "default", "alias": None}], "source": None}]}
    assert export_entries(generic) == ["a", "b as bee", "default"]
    assert export_entries({}) == []


# ── record helpers ────────────────────────────────────────────────────

def test_unresolved_calls_hide_builtins_but_keep_dependencies():
    from src.structure.records import is_noise_call
    for noise in ("len", "str", "isinstance", "norm.rstrip", "items.append", "console.log",
                  "JSON.stringify", "logger.info", "require", "arr.map", "callback_L12_C5"):
        assert is_noise_call(noise), noise
    for real in ("db.update", "axios.get", "router.get", "json.dumps", "fetch", "collection.find",
                 "self.save", "super().execute", "Repo"):
        assert not is_noise_call(real), real


def test_import_statements_prefer_full_statements_in_source_order():
    from src.structure.records import import_statements
    meta = {
        "imports": ["os", "import os", "from x import y", "x", "import os"],
        "import_lines": {"import os": 5, "os": 5, "from x import y": 2, "x": 2},
    }
    assert import_statements(meta) == ["from x import y", "import os"]
    assert import_statements({"imports": ["@trpc/server", "react"]}) == ["@trpc/server", "react"]
    multi = {"imports": ["import type {\n  A,\n  B,\n} from './t';", "./t"]}
    assert import_statements(multi) == ["import type { A, B } from './t';"]


# ── health meta ───────────────────────────────────────────────────────

def test_meta_reports_stale_index_and_unbuilt_graph(client):
    client.stale = True
    client.built = False
    meta = CodeStructure(client).map(".")["meta"]
    assert meta["index_stale"] is True and meta["graph_built"] is False
    assert len(meta["warnings"]) == 2


def test_meta_includes_health_callback_warnings(client):
    s = CodeStructure(client, health=lambda: ["sync failed for x.py"])
    assert "sync failed for x.py" in s.lookup("write_db")["meta"]["warnings"]
