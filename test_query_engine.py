"""
Tests for the Query Engine & DSL.
"""

# pyrefly: ignore [missing-import]
import pytest
# pyrefly: ignore [missing-import]
from src.query.parser import parse_query, GetQuery, MetadataQuery, ImpactQuery, Condition


class TestParser:
    def test_get_simple(self):
        q = parse_query("GET functions WHERE name LIKE 'test_*' WITH callers LIMIT 20")
        assert isinstance(q, GetQuery)
        assert q.type_filter == "function"
        assert len(q.conditions) == 1
        assert q.conditions[0].field == "name"
        assert q.conditions[0].operator == "LIKE"
        assert q.conditions[0].value == "test_*"
        assert q.graph_op == "callers"
        assert q.limit == 20
        assert q.depth == 0

    def test_get_clause_order_independence(self):
        q = parse_query("GET functions WHERE name LIKE 'test_*' LIMIT 20 WITH callers")
        assert q.graph_op == "callers"
        assert q.limit == 20

        q = parse_query("GET functions WHERE name LIKE 'test_*' DEPTH 3 LIMIT 20 WITH callers")
        assert q.graph_op == "callers"
        assert q.limit == 20
        assert q.depth == 3

    def test_get_limit_all(self):
        """LIMIT ALL / LIMIT * parse to the UNLIMITED sentinel (-1), not a page size."""
        from src.query.parser import UNLIMITED
        q = parse_query("GET functions WHERE name LIKE 'test_*' LIMIT ALL")
        assert q.limit == UNLIMITED
        q2 = parse_query("GET functions WHERE name LIKE 'test_*' LIMIT *")
        assert q2.limit == UNLIMITED
        q3 = parse_query("SEARCH 'foo' LIMIT ALL")
        assert q3.limit == UNLIMITED
        q4 = parse_query("GLOB '**/*.py' LIMIT ALL")
        assert q4.limit == UNLIMITED

    def test_get_without_type(self):
        q = parse_query("GET ALL WHERE name LIKE 'test' LIMIT 5")
        assert isinstance(q, GetQuery)
        assert q.type_filter is None
        assert q.limit == 5

    def test_get_asterisk_type(self):
        q = parse_query("GET * WHERE name LIKE 'test'")
        assert isinstance(q, GetQuery)
        assert q.type_filter is None

    def test_get_multiple_conditions(self):
        q = parse_query("GET functions WHERE type == 'Function' AND name LIKE 'get_*' WITH callees")
        assert isinstance(q, GetQuery)
        assert len(q.conditions) == 2
        assert q.conditions[0].field == "type"
        assert q.conditions[0].operator == "=="
        assert q.conditions[0].value == "Function"
        assert q.conditions[1].field == "name"
        assert q.conditions[1].operator == "LIKE"
        assert q.conditions[1].value == "get_*"
        assert q.graph_op == "callees"

    def test_get_single_equals_is_exact_match(self):
        """WHERE field = 'value' must parse as an exact == match (alias)."""
        q = parse_query("GET functions WHERE file_path = 'src/modules/comptabilite/services.py'")
        assert isinstance(q, GetQuery)
        assert len(q.conditions) == 1
        assert q.conditions[0].field == "file_path"
        assert q.conditions[0].operator == "="
        assert q.conditions[0].value == "src/modules/comptabilite/services.py"
        # '=' must also work for constant literals (WHERE 1=1)
        q2 = parse_query("GET functions WHERE 1=1")
        assert q2.conditions[0].field == "__constant__"
        assert q2.conditions[0].value is True

    def test_get_plural_types(self):
        for plural, singular in [("functions", "function"), ("classes", "class"),
                                  ("files", "file"), ("folders", "folder"), ("routes", "route"),
                                  ("modules", "module"), ("packages", "package")]:
            q = parse_query(f"GET {plural} WHERE name LIKE 'test'")
            assert q.type_filter == singular, f"{plural} -> {singular}"

    def test_get_singular_types(self):
        for t in ("function", "class", "file", "folder", "route", "module", "package"):
            q = parse_query(f"GET {t} WHERE name LIKE 'test'")
            assert q.type_filter == t

    def test_get_from_modules_syntax(self):
        """GET ... FROM modules / packages must parse (regression for parse error)."""
        for t in ("modules", "packages"):
            q = parse_query(f"GET * FROM {t}")
            assert q.type_filter == t[:-1], f"GET * FROM {t} -> {q.type_filter}"
            assert q.projection is None

    def test_get_class_plural(self):
        q = parse_query("GET classes WHERE name LIKE 'test'")
        assert q.type_filter == "class"

    def test_get_all_operators(self):
        for op in ("==", "!=", "LIKE", "CONTAINS", "STARTSWITH", "ENDSWITH"):
            q = parse_query(f"GET functions WHERE name {op} 'value'")
            assert q.conditions[0].operator == op

    def test_get_without_conditions(self):
        q = parse_query("GET functions WHERE name LIKE '%' LIMIT 10")
        assert isinstance(q, GetQuery)
        assert q.type_filter == "function"
        assert len(q.conditions) == 1

    def test_get_with_where_only(self):
        """At minimum, GET must have WHERE with at least one condition."""
        q = parse_query("GET functions WHERE name LIKE '%'")
        assert isinstance(q, GetQuery)
        assert q.graph_op is None
        assert q.limit is None

    def test_metadata_query(self):
        q = parse_query("METADATA FOR 'src/modules/sales/api.py:get_sales'")
        assert isinstance(q, MetadataQuery)
        assert q.node_id == "src/modules/sales/api.py:get_sales"

    def test_metadata_double_quotes(self):
        q = parse_query('METADATA FOR "src/modules/sales/api.py:get_sales"')
        assert isinstance(q, MetadataQuery)
        assert q.node_id == "src/modules/sales/api.py:get_sales"

    def test_impact_query_defaults(self):
        q = parse_query("IMPACT OF 'src/modules/sales/api.py:get_sales'")
        assert isinstance(q, ImpactQuery)
        assert q.node_id == "src/modules/sales/api.py:get_sales"
        assert q.direction == "callers"
        assert q.depth == 0

    def test_impact_query_full(self):
        q = parse_query("IMPACT OF 'src/modules/sales/api.py:get_sales' DIRECTION callees DEPTH 3")
        assert isinstance(q, ImpactQuery)
        assert q.node_id == "src/modules/sales/api.py:get_sales"
        assert q.direction == "callees"
        assert q.depth == 3

    def test_impact_query_direction_callers(self):
        q = parse_query("IMPACT OF 'foo.py:bar' DIRECTION callers DEPTH 2")
        assert q.direction == "callers"
        assert q.depth == 2

    def test_empty_query_raises(self):
        with pytest.raises(ValueError, match="Empty query"):
            parse_query("")

    def test_whitespace_handling(self):
        q = parse_query("  GET functions WHERE name LIKE 'test'  ")
        assert isinstance(q, GetQuery)
        assert q.type_filter == "function"

    def test_file_path_field(self):
        q = parse_query("GET functions WHERE file_path LIKE 'src/modules/*'")
        assert q.conditions[0].field == "file_path"
        assert q.conditions[0].value == "src/modules/*"

    def test_double_quoted_value(self):
        q = parse_query('GET functions WHERE name LIKE "test_*"')
        assert q.conditions[0].value == "test_*"

    def test_single_quoted_value(self):
        q = parse_query("GET functions WHERE name LIKE 'test_*'")
        assert q.conditions[0].value == "test_*"

    def test_field_mappings(self):
        q = parse_query("GET functions WHERE name LIKE 'x'")
        assert q.conditions[0].field == "name"
        q = parse_query("GET functions WHERE type == 'Function'")
        assert q.conditions[0].field == "type"
        q = parse_query("GET functions WHERE file_path LIKE 'x'")
        assert q.conditions[0].field == "file_path"
        q = parse_query("GET functions WHERE signature LIKE 'x'")
        assert q.conditions[0].field == "signature"
        q = parse_query("GET functions WHERE docstring LIKE 'x'")
        assert q.conditions[0].field == "docstring"

    def test_case_insensitive_keywords(self):
        q1 = parse_query("get functions WHERE name LIKE 'test'")
        q2 = parse_query("GET functions WHERE name LIKE 'test'")
        assert q1.type_filter == q2.type_filter
        # Also test lowercase query type
        q3 = parse_query("get function where name like 'test'")
        assert isinstance(q3, GetQuery)
        assert q3.type_filter == "function"

    def test_metadata_dot_in_node_id(self):
        q = parse_query("METADATA FOR 'src/modules/core/models.py:Shop'")
        assert q.node_id == "src/modules/core/models.py:Shop"

    def test_get_with_depth_and_limit(self):
        q = parse_query("GET functions WHERE name LIKE 'test' WITH callers LIMIT 10 DEPTH 3")
        assert q.limit == 10
        assert q.depth == 3
        assert q.graph_op == "callers"

    def test_get_with_depth_no_limit(self):
        q = parse_query("GET functions WHERE name LIKE 'test' WITH callers DEPTH 3")
        assert q.limit is None
        assert q.depth == 3
        assert q.graph_op == "callers"

    def test_get_with_tree(self):
        q = parse_query("GET functions WHERE name LIKE 'test' WITH tree")
        assert q.graph_op == "tree"

    def test_impact_without_options(self):
        q = parse_query("IMPACT OF 'foo:bar'")
        assert isinstance(q, ImpactQuery)
        assert q.node_id == "foo:bar"
        assert q.direction == "callers"
        assert q.depth == 0

    def test_path_query_parsing(self):
        from src.query.parser import PathQuery
        q = parse_query("PATH FROM 'api.py:create_sale' TO 'models.py:Shop'")
        assert isinstance(q, PathQuery)
        assert q.start_node == "api.py:create_sale"
        assert q.end_node == "models.py:Shop"

    def test_search_regex_keyword(self):
        from src.query.parser import SearchQuery
        q = parse_query('SEARCH REGEX "def \\\\w+" IN functions')
        assert isinstance(q, SearchQuery)
        assert q.is_regex is True
        assert len(q.patterns) == 1
        assert "def" in q.patterns[0]
        assert q.target_type == "function"

        # Also test without IN clause
        q2 = parse_query('SEARCH REGEX "pattern"')
        assert q2.is_regex is True
        assert q2.patterns == ["pattern"]


def _flatten_results(res: dict) -> list[dict]:
    """Flatten the grouped GET result dict back into a list of item dicts for assertions."""
    raw = res.get("results", [])
    if isinstance(raw, dict):
        items = []
        for fp, entries in raw.items():
            if not isinstance(entries, list):
                entries = [entries]
            for entry in entries:
                if isinstance(entry, str) and ":" in entry:
                    name, _, span = entry.partition(":")
                    span = span.strip()
                    start, _, end = span.partition("-")
                    items.append({
                        "name": name.strip(),
                        "file_path": fp,
                        "lines_start": int(start) if start.strip().isdigit() else 0,
                        "lines_end": int(end) if end.strip().isdigit() else 0,
                    })
                elif isinstance(entry, str):
                    # bare number (file node), no name prefix
                    items.append({"name": fp.split("/")[-1] if "/" in fp else fp, "file_path": fp, "lines_count": int(entry) if entry.strip().isdigit() else 0})
                elif isinstance(entry, dict):
                    entry["file_path"] = fp
                    items.append(entry)
        return items
    return raw


class TestCompileGet:
    def test_basic_query(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        result = query(db.client, "GET functions WHERE name LIKE 'get_*' LIMIT 3")
        assert result["meta"]["ok"] is True
        assert result["meta"]["type"] == "function"
        items = _flatten_results(result)
        assert len(items) <= 3

    def test_query_with_graph_op(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        result = query(db.client, "GET functions WHERE name LIKE 'test_*' WITH callers LIMIT 3")
        assert result["meta"]["ok"] is True
        if result["results"]:
            assert "related_nodes" in result["results"][0]

    def test_metadata_query(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        all_meta = db.client.get_all_metadata()
        # Find any function node
        test_id = None
        for nid, meta in all_meta.items():
            meta_dict = dict(meta) if hasattr(meta, "items") else meta
            if meta_dict.get("type") == "Function" and test_id is None:
                test_id = nid
                break
        if test_id:
            result = query(db.client, f"METADATA FOR '{test_id}'")
            assert result["meta"]["ok"] is True
            assert result["meta"]["query_type"] == "METADATA"
            assert result["results"]["node"] is not None
            assert "callers_count" in result["meta"]
            assert "callees_count" in result["meta"]

    def test_impact_query_callers(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        all_meta = db.client.get_all_metadata()
        test_id = None
        for nid, meta in all_meta.items():
            meta_dict = dict(meta) if hasattr(meta, "items") else meta
            if meta_dict.get("type") == "Function" and test_id is None:
                test_id = nid
                break
        if test_id:
            result = query(db.client, f"IMPACT OF '{test_id}' DIRECTION callers DEPTH 1")
            assert result["meta"]["ok"] is True
            assert result["meta"]["query_type"] == "IMPACT"
            assert result["meta"]["direction"] == "callers"
            assert result["meta"]["target_node_id"] == test_id
            assert "architecture" in result["results"]

    def _seed_impact_chain(self, tmpdir):
        """fn_a -> fn_b -> fn_c -> fn_d call chain (callees direction)."""
        from src.database.graph_client import EngramClient
        client = EngramClient(tmpdir)
        nxt = {"a": "b", "b": "c", "c": "d"}
        for name in ("a", "b", "c"):
            client.add_node(f"{name}.ts", "File", f"{name}.ts", f"{name}.ts")
            client.add_node(f"{name}.ts:fn_{name}", "Function", f"fn_{name}", f"{name}.ts",
                            lines={"start": 1, "end": 5}, calls=[f"fn_{nxt[name]}"])
        client.add_node("d.ts", "File", "d.ts", "d.ts")
        client.add_node("d.ts:fn_d", "Function", "fn_d", "d.ts", lines={"start": 1, "end": 5})
        client.build()
        for name in ("a", "b", "c"):
            client.resolve_and_connect_calls(f"{name}.ts:fn_{name}", [f"fn_{nxt[name]}"])
        client.rebuild()
        return client

    def test_impact_mode_count(self):
        """MODE count returns aggregate numbers only — no per-file listings."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed_impact_chain(tmpdir)
            res = query(client, "IMPACT OF 'a.ts:fn_a' DIRECTION callees MODE count")
            assert res["meta"]["ok"] is True
            assert res["meta"]["mode"] == "count"
            r = res["results"]
            assert set(r.keys()) == {"total", "by_type", "by_module", "hint"}
            assert r["total"] == res["meta"]["count"]
            assert isinstance(r["by_type"], dict) and isinstance(r["by_module"], dict)
            assert "affected_nodes" not in r
            # ~token safety: no file listing payload
            assert len(str(r)) < 600
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_impact_mode_summary(self):
        """MODE summary groups blast radius by type + top-level module and lists
        direct callers/callees without the full transitive listing."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed_impact_chain(tmpdir)
            res = query(client, "IMPACT OF 'a.ts:fn_a' DIRECTION callees MODE summary")
            assert res["meta"]["ok"] is True
            assert res["meta"]["mode"] == "summary"
            r = res["results"]
            assert r["total"] == res["meta"]["count"]
            fn_group = r["by_type"].get("Function") or next(iter(r["by_type"].values()))
            assert fn_group["count"] == res["meta"]["count"]
            assert fn_group["modules"] == {"(root)": res["meta"]["count"]}
            direct_key = "direct_callees"
            assert direct_key in r and isinstance(r[direct_key], list)
            assert "affected_nodes" not in r
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_impact_mode_detailed_default(self):
        """Default (no MODE) keeps the detailed shape; explicit MODE detailed matches."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed_impact_chain(tmpdir)
            base = query(client, "IMPACT OF 'a.ts:fn_a' DIRECTION callees")
            explicit = query(client, "IMPACT OF 'a.ts:fn_a' DIRECTION callees MODE detailed")
            assert base["meta"]["ok"] and explicit["meta"]["ok"]
            assert base["results"] == explicit["results"]
            assert "affected_nodes" in base["results"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_impact_query_callees(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        all_meta = db.client.get_all_metadata()
        test_id = None
        for nid, meta in all_meta.items():
            meta_dict = dict(meta) if hasattr(meta, "items") else meta
            if meta_dict.get("type") == "Function" and test_id is None:
                test_id = nid
                break
        if test_id:
            result = query(db.client, f"IMPACT OF '{test_id}' DIRECTION callees DEPTH 1")
            assert result["meta"]["ok"] is True
            assert result["meta"]["direction"] == "callees"
            assert result["meta"]["target_node_id"] == test_id
            assert "callees_count" in result["meta"]
            assert "callers_count" not in result["meta"]
            if result["results"]["affected_nodes"]:
                # Compact: affected_nodes is a {file_path: ["Name: start-end"]} dict
                assert isinstance(result["results"]["affected_nodes"], dict)

    def test_impact_meta_reconciles_with_metadata_counts(self):
        """IMPACT direct counts must match METADATA callers/callees counts (same
        graph edge source), so a transitive blast radius is never mistaken for
        the direct caller/callee set."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py")
            client.add_node("b.py:bar", "Function", "bar", "b.py")
            client.add_node("c.py:baz", "Function", "baz", "c.py")
            client.add_edge("a.py:foo", "b.py:bar")
            client.add_edge("b.py:bar", "c.py:baz")
            client.build()

            md = query(client, "METADATA FOR 'a.py:foo'")
            assert md["meta"]["callees_count"] == 1

            r = query(client, "IMPACT OF 'a.py:foo' DIRECTION callees DEPTH 1")
            assert r["meta"]["callees_count"] == 1
            assert r["meta"]["count"] == 1

            md = query(client, "METADATA FOR 'c.py:baz'")
            assert md["meta"]["callers_count"] == 1

            r = query(client, "IMPACT OF 'c.py:baz' DIRECTION callers DEPTH 1")
            assert r["meta"]["callers_count"] == 1
            assert r["meta"]["count"] == 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_on_db_instance(self):
        from src.database import get_graph_db
        db = get_graph_db()
        result = db.query("GET functions WHERE name LIKE 'get_*' LIMIT 2")
        assert result["meta"]["ok"] is True
        assert len(_flatten_results(result)) <= 2

    def test_nonexistent_node_metadata(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        result = query(db.client, "METADATA FOR 'nonexistent_node_xyz'")
        assert result["ok"] is False

    def test_nonexistent_node_impact(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        result = query(db.client, "IMPACT OF 'nonexistent_node_xyz'")
        assert result["ok"] is False

    def test_invalid_syntax(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        result = query(db.client, "INVALID QUERY HERE")
        assert result["ok"] is False
        assert "Parse error" in result["error"]
        assert 'Start with: STATS' in result["error"]

    def test_parse_error_includes_command_specific_example(self):
        from src.query import query
        from src.database import get_graph_db

        result = query(get_graph_db().client, "IMPACT broken")
        assert result["ok"] is False
        assert 'Example: IMPACT OF "src/services.py:create_sale"' in result["error"]

    def test_empty_query(self):
        from src.query import query
        from src.database import get_graph_db
        db = get_graph_db()
        result = query(db.client, "")
        assert result["ok"] is False
        assert "Parse error" in result["error"]

    def test_path_query_compile(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py")
            client.add_node("b.py:bar", "Function", "bar", "b.py")
            client.add_node("c.py:baz", "Function", "baz", "c.py")

            client.add_edge("a.py:foo", "b.py:bar")
            client.add_edge("b.py:bar", "c.py:baz")
            client.build()

            # Shorthand path search
            result = query(client, "PATH FROM 'foo' TO 'baz'")
            assert result["meta"]["ok"] is True
            assert result["meta"]["found"] is True
            assert [r["node_id"] for r in result["results"]] == ["a.py:foo", "b.py:bar", "c.py:baz"]
            assert result["results"][0]["edge_to_next"] == "CALLS"
            assert "edge_to_next" not in result["results"][-1]

            # Nonexistent path
            result = query(client, "PATH FROM 'baz' TO 'foo'")
            assert result["meta"]["ok"] is True
            assert result["meta"]["found"] is False
            assert result["results"] == []
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_relation_filter(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            # Add a class with ForeignKey to Shop
            client.add_node(
                "models.py:Purchase", "Class", "Purchase", "models.py",
                django_relations=[{"related_model": "Shop", "relation_type": "ForeignKey"}]
            )
            # Add a class with OneToOneField to Shop
            client.add_node(
                "models.py:Manager", "Class", "Manager", "models.py",
                django_relations=[{"related_model": "Shop", "relation_type": "OneToOneField"}]
            )
            # Add a class with ForeignKey to Product
            client.add_node(
                "models.py:OrderItem", "Class", "OrderItem", "models.py",
                django_relations=[{"related_model": "Product", "relation_type": "ForeignKey"}]
            )
            client.build()

            # Query relations targeting Shop
            result = query(client, "GET classes WHERE relation.target == 'Shop'")
            assert result["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(result)]
            assert len(names) == 2
            assert "Purchase" in names
            assert "Manager" in names

            # Query ForeignKey relations targeting Shop
            result = query(client, "GET classes WHERE relation.target == 'Shop' AND relation.type == 'ForeignKey'")
            assert result["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(result)]
            assert names == ["Purchase"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_with_depth_no_limit_compile(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py")
            client.add_node("b.py:bar", "Function", "bar", "b.py")
            client.add_node("c.py:baz", "Function", "baz", "c.py")

            client.add_edge("a.py:foo", "b.py:bar")
            client.add_edge("b.py:bar", "c.py:baz")
            client.build()

            # Depth 1 from foo should only enrich with callers/callees at depth 1
            result = query(client, "GET functions WHERE name == 'foo' WITH callees DEPTH 1")
            assert result["meta"]["ok"] is True
            related = [r["node_id"] for r in result["results"][0]["related_nodes"]]
            assert "b.py:bar" in related
            assert "c.py:baz" not in related

            # Depth 2 should find both
            result = query(client, "GET functions WHERE name == 'foo' WITH callees DEPTH 2")
            assert result["meta"]["ok"] is True
            related = [r["node_id"] for r in result["results"][0]["related_nodes"]]
            assert "b.py:bar" in related
            assert "c.py:baz" in related
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_body_and_new_operators(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py", _extra={"body": "def foo():\n    return 'hello'"})
            client.add_node("b.py:bar", "Function", "bar", "b.py", _extra={"body": "def bar():\n    raise ValueError('error')"})
            client.add_node("c.py:test_baz", "Function", "test_baz", "c.py", _extra={"body": "def test_baz():\n    pass"})
            client.add_node("config.json", "File", "config.json", "config.json", _extra={"body": '{"env": "prod"}'})
            client.build()

            # 1. Test body CONTAINS
            result = query(client, "GET functions WHERE body CONTAINS 'ValueError'")
            assert result["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(result)]
            assert names == ["bar"]

            # 2. Test regex =~ operator
            result = query(client, "GET functions WHERE name =~ '^test_'")
            assert result["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(result)]
            assert names == ["test_baz"]

            # 3. Test regex MATCHES operator
            result = query(client, "GET functions WHERE name MATCHES 'baz$'")
            assert result["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(result)]
            assert names == ["test_baz"]

            # 4. Test NOT LIKE operator
            result = query(client, "GET functions WHERE name NOT LIKE 'test_*'")
            assert result["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(result)]
            assert "test_baz" not in names
            assert "foo" in names
            assert "bar" in names

            # 5. Test file type suffix matching
            result = query(client, "GET files WHERE file_path ENDSWITH '.json'")
            assert result["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(result)]
            assert "config.json" in names

            # 6. Test GET * FROM and GET FROM syntax
            res_star = query(client, "GET * FROM files LIMIT 30")
            assert res_star["meta"]["ok"] is True
            assert any(r["name"] == "config.json" for r in _flatten_results(res_star))

            # Single '=' is an exact-match alias for '=='
            res_eq = query(client, 'GET name FROM functions WHERE file_path = "a.py"')
            assert res_eq["meta"]["ok"] is True
            assert res_eq["meta"]["total"] == 1
            assert [r["name"] for r in _flatten_results(res_eq)] == ["foo"]

            res_from = query(client, "GET FROM functions WHERE name == 'foo'")
            assert res_from["meta"]["ok"] is True
            items = _flatten_results(res_from)
            assert len(items) == 1
            assert items[0]["name"] == "foo"

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_default_limit_guardrail(self):
        """GET without LIMIT defaults to DEFAULT_PAGE_SIZE; LIMIT ALL bypasses the cap."""
        from src.query import query
        from src.query.compiler import DEFAULT_PAGE_SIZE
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            for i in range(130):
                client.add_node(f"a.py:func{i}", "Function", f"func{i}", "a.py")
            client.build()

            # 1. Default page size (no LIMIT clause) returns DEFAULT_PAGE_SIZE results
            result = query(client, "GET functions WHERE name LIKE 'func*'")
            assert result["meta"]["ok"] is True
            assert len(_flatten_results(result)) == DEFAULT_PAGE_SIZE
            assert result["meta"]["count"] == DEFAULT_PAGE_SIZE
            assert result["meta"]["total"] == 130
            assert result["meta"]["truncated"] is True
            assert "hint" in result["meta"]

            # 2. Explicit limit overrides default
            result = query(client, "GET functions WHERE name LIKE 'func*' LIMIT 5")
            assert result["meta"]["ok"] is True
            assert len(_flatten_results(result)) == 5

            # 3. Large explicit limits are capped but return everything when under the cap
            result = query(client, "GET functions WHERE name LIKE 'func*' LIMIT 2000")
            assert result["meta"]["count"] == 130
            assert result["meta"]["truncated"] is False

            # 4. LIMIT ALL / LIMIT * bypass the cap entirely — one query, no pagination
            result_all = query(client, "GET functions WHERE name LIKE 'func*' LIMIT ALL")
            assert result_all["meta"]["ok"] is True
            assert result_all["meta"]["count"] == 130
            assert result_all["meta"]["truncated"] is False
            assert "hint" not in result_all["meta"]
            result_star = query(client, "GET functions WHERE name LIKE 'func*' LIMIT *")
            assert result_star["meta"]["count"] == 130

            # 5. OFFSET and RANGE Pagination
            # Test OFFSET 100 LIMIT 30
            page2 = query(client, "GET functions WHERE name LIKE 'func*' OFFSET 100 LIMIT 30")
            assert page2["meta"]["ok"] is True
            assert page2["meta"]["offset"] == 100
            assert page2["meta"]["count"] == 30
            assert page2["meta"]["truncated"] is False

            # Test RANGE 100:130 (start 100, end 130 -> offset 100, limit 30)
            range_page = query(client, "GET functions WHERE name LIKE 'func*' RANGE 100:130")
            assert range_page["meta"]["ok"] is True
            assert range_page["meta"]["offset"] == 100
            assert range_page["meta"]["count"] == 30
            assert [r["name"] for r in _flatten_results(range_page)] == [r["name"] for r in _flatten_results(page2)]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_boolean_logic(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:f1", "Function", "f1", "a.py", is_async=True, param_count=2)
            client.add_node("a.py:f2", "Function", "f2", "a.py", is_async=False, param_count=10)
            client.add_node("test_a.py:f3", "Function", "f3", "test_a.py", is_async=True, param_count=10)
            client.build()

            # (is_async == true OR param_count > 5) AND NOT file LIKE '*test*'
            r = query(client, "GET functions WHERE (is_async == true OR param_count > 5) AND NOT file LIKE '*test*'")
            assert r["meta"]["ok"] is True
            names = [n["name"] for n in _flatten_results(r)]
            assert "f1" in names
            assert "f2" in names
            assert "f3" not in names
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_projection_and_order_by(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:f1", "Function", "f1", "a.py", param_count=2, lines={"start": 1, "end": 10})
            client.add_node("a.py:f2", "Function", "f2", "a.py", param_count=5, lines={"start": 1, "end": 100})
            client.add_node("a.py:f3", "Function", "f3", "a.py", param_count=1, lines={"start": 1, "end": 50})
            client.build()

            # 1. ORDER BY lines_count DESC
            r = query(client, "GET name, lines_count functions ORDER BY lines_count DESC")
            assert r["meta"]["ok"] is True
            results_list = _flatten_results(r)
            res_names = [item["name"] for item in results_list]
            assert res_names == ["f2", "f3", "f1"]
            assert "name" in results_list[0] and "lines_count" in results_list[0]

            # 2. COUNT(*) aggregation
            cnt = query(client, "GET COUNT(*) functions")
            assert cnt["meta"]["ok"] is True
            assert cnt["meta"]["count"] == 3
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_search_syntax(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:f1", "Function", "auth_login", "a.py", _extra={"body": "def auth_login(): pass"})
            client.add_node("b.py:f2", "Function", "process_payment", "b.py", _extra={"body": "def process_payment(): atomic()"})
            client.build()

            # SEARCH "atomic" IN functions
            res = query(client, 'SEARCH "atomic" IN functions')
            assert res["meta"]["ok"] is True
            assert res["meta"]["query_type"] == "SEARCH"
            assert res["meta"]["total"] == 1
            assert _flatten_results(res)[0]["name"] == "process_payment"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_search_regex_flags(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:f1", "Function", "auth_login", "a.py", _extra={"body": "def auth_login(): pass"})
            client.add_node("b.py:f2", "Function", "AUTH_REGISTER", "b.py", _extra={"body": "def AUTH_REGISTER(): pass"})
            client.build()

            # 1. Plain substring search
            res1 = query(client, 'SEARCH "auth_login" IN functions')
            assert res1["meta"]["ok"] is True
            assert res1["meta"]["total"] == 1
            assert _flatten_results(res1)[0]["name"] == "auth_login"

            # 2. Regex search with /i flag
            res2 = query(client, 'SEARCH /def\\s+auth_\\w+/i IN functions')
            assert res2["meta"]["ok"] is True
            assert res2["meta"]["total"] == 2

            # 3. Regex search case-sensitive (no /i flag)
            res3 = query(client, 'SEARCH /def\\s+AUTH_\\w+/ IN functions')
            assert res3["meta"]["ok"] is True
            assert res3["meta"]["total"] == 1
            assert _flatten_results(res3)[0]["name"] == "AUTH_REGISTER"

            # 4. SEARCH REGEX "explicit pattern" — quoted string treated as regex
            res4 = query(client, 'SEARCH REGEX "auth_login" IN functions')
            assert res4["meta"]["ok"] is True
            assert res4["meta"]["total"] == 1
            assert _flatten_results(res4)[0]["name"] == "auth_login"

            # 5. SEARCH REGEX with regex metacharacters
            res5 = query(client, 'SEARCH REGEX "def .+:" IN functions')
            assert res5["meta"]["ok"] is True
            assert res5["meta"]["total"] == 2

            # 5. SEARCH REGEX with case-sensitive literal match
            res5 = query(client, 'SEARCH REGEX "AUTH_REGISTER" IN functions')
            assert res5["meta"]["ok"] is True
            assert res5["meta"]["total"] == 1
            assert _flatten_results(res5)[0]["name"] == "AUTH_REGISTER"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_glob_syntax(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/modules/sales/services.py", "File", "services.py", "src/modules/sales/services.py")
            client.add_node("src/modules/catalog/models.py", "File", "models.py", "src/modules/catalog/models.py")
            client.add_node("src/config.json", "File", "config.json", "src/config.json")
            client.build()

            res = query(client, 'GLOB "**/*.py"')
            meta = res["meta"]
            assert meta["ok"] is True
            assert meta["type"] == "file"
            assert meta["total"] == 2

            res2 = query(client, 'GLOB "src/modules/*/*.py" WHERE file CONTAINS "sales"')
            assert res2["meta"]["ok"] is True
            assert res2["meta"]["total"] == 1
            assert list(res2["results"].keys()) == ["src/modules/sales/services.py"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_flow_stack_audit_syntax(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:f1", "Function", "auth_login", "a.py")
            client.build()

            res_flow = query(client, "FLOW FOR 'a.py:f1' DEPTH 2")
            # FLOW/STACK trace the global DB, not the temp client → flat error dict here
            assert res_flow.get("query_type") == "FLOW"

            res_stack = query(client, "STACK FOR '/api/sales'")
            assert res_stack.get("query_type") == "STACK"

            res_audit = query(client, "AUDIT TENANT sales")
            assert res_audit.get("meta", {}).get("query_type") == "AUDIT"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_general_project_architecture(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil
        import os

        tmpdir = tempfile.mkdtemp()
        try:
            original_pytest = os.environ.get("PYTEST_CURRENT_TEST")
            if "PYTEST_CURRENT_TEST" in os.environ:
                del os.environ["PYTEST_CURRENT_TEST"]
                
            client = EngramClient(tmpdir)
            client.add_node("models.py:User", "Class", "User", "models.py")
            client.add_node("services.py:get_user", "Function", "get_user", "services.py")
            client.build()

            # Query User model
            result = query(client, "IMPACT OF 'models.py:User' DIRECTION callers")
            assert result["meta"]["ok"] is True
            assert "architecture" in result["results"]
            arch = result["results"]["architecture"]
            assert arch["architecture_role"] == "Model"
            # Since this is generic, we should have no warnings about TenantAwareModel
            assert "architectural_warnings" not in arch

            # Query get_user service
            result_service = query(client, "IMPACT OF 'services.py:get_user' DIRECTION callers")
            arch_service = result_service["results"]["architecture"]
            assert arch_service["architecture_role"] == "Service"
            # Since this is generic, we should have no warnings about missing shop argument
            assert "architectural_warnings" not in arch_service

        finally:
            if original_pytest:
                os.environ["PYTEST_CURRENT_TEST"] = original_pytest
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_body_truncation_and_expansion(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            long_body = "x" * 200
            client.add_node("a.py:foo", "Function", "foo", "a.py", _extra={"body": long_body})
            client.build()

            # Compact format: results are "name: start-end" strings, body not included
            result = query(client, "GET functions WHERE name == 'foo'", expand_body=False)
            assert result["meta"]["ok"] is True
            items = _flatten_results(result)
            assert len(items) == 1
            assert items[0]["name"] == "foo"

            result_expanded = query(client, "GET functions WHERE name == 'foo'", expand_body=True)
            assert result_expanded["meta"]["ok"] is True
            items = _flatten_results(result_expanded)
            assert len(items) == 1
            assert items[0]["name"] == "foo"

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


    def test_path_query_frontend_to_backend(self):
        from src.query import query
        from src.watcher.sync_handler import GraphSyncHandler
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil
        import os

        ws = tempfile.mkdtemp(prefix="test_path_fullstack_")
        old_environ = dict(os.environ)
        os.environ["WORKSPACE_PATH"] = ws
        try:
            # 1. Create React hook ts file
            hooks_dir = os.path.join(ws, "frontend", "ignite-pos", "src", "hooks")
            os.makedirs(hooks_dir, exist_ok=True)
            with open(os.path.join(hooks_dir, "use-sales.ts"), "w") as f:
                f.write("""
export function useCompleteSale() {
    const res = apiFetch("/sales/complete", { method: 'POST' });
    return res;
}
""")

            # 2. Create Python API file
            sales_dir = os.path.join(ws, "src", "modules", "sales")
            os.makedirs(sales_dir, exist_ok=True)
            with open(os.path.join(sales_dir, "api.py"), "w") as f:
                f.write("""
from . import services

def complete_sale(request):
    return services.complete_sale()
""")

            # 3. Create Python Service file
            with open(os.path.join(sales_dir, "services.py"), "w") as f:
                f.write("""
def complete_sale():
    return True
""")

            from src.database import get_graph_db, _db_instances
            key = os.path.abspath(ws)
            if key in _db_instances:
                del _db_instances[key]

            handler = GraphSyncHandler(ws)
            db = get_graph_db(ws)
            for root, dirs, files in os.walk(ws):
                for file in files:
                    if file.endswith(handler.supported_extensions):
                        p = os.path.join(root, file)
                        parsed = handler.parser.parse_file(p)
                        handler.update_file_in_graph(p, skip_rebuild=True, pre_parsed_data=parsed)

            db.client.repopulate_edges()
            db.client.resolve_api_calls()
            db.client.build()

            start_node = "frontend/ignite-pos/src/hooks/use-sales.ts:useCompleteSale"
            end_node = "src/modules/sales/services.py:complete_sale"

            res = query(db.client, f'PATH FROM "{start_node}" TO "{end_node}"')
            assert res["meta"]["ok"] is True
            assert res["meta"]["found"] is True
            assert res["meta"]["length"] >= 1
            path_nodes = [n["node_id"] for n in res["results"]]
            assert start_node in path_nodes
            assert end_node in path_nodes
        finally:
            os.environ.clear()

    def test_precomputed_metrics_and_numeric_queries(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            # Add nodes with line ranges
            client.add_node("a.py:god_func", "Function", "god_func", "a.py", lines={'start': 1, 'end': 100})
            client.add_node("b.py:helper1", "Function", "helper1", "b.py", lines={'start': 10, 'end': 20})
            client.add_node("c.py:helper2", "Function", "helper2", "c.py", lines={'start': 1, 'end': 15})
            client.add_node("d.py:dead_func", "Function", "dead_func", "d.py", lines={'start': 1, 'end': 5})

            # Connect edges: god_func calls helper1 and helper2
            client.add_edge("a.py:god_func", "b.py:helper1")
            client.add_edge("a.py:god_func", "c.py:helper2")
            client.build()

            # Verify precomputed metrics in engine metadata
            god_meta = client.get_node_meta("a.py:god_func")
            assert god_meta["calls_count"] == 2
            assert god_meta["callers_count"] == 0
            assert god_meta["lines_count"] == 100

            helper1_meta = client.get_node_meta("b.py:helper1")
            assert helper1_meta["calls_count"] == 0
            assert helper1_meta["callers_count"] == 1
            assert helper1_meta["lines_count"] == 11

            # 1. Test God Functions: calls_count > 1
            res = query(client, "GET functions WHERE calls_count > 1")
            assert res["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(res)]
            assert names == ["god_func"]

            # 2. Test Dead Code / Entry Points: callers_count == 0
            res = query(client, "GET functions WHERE callers_count == 0")
            assert res["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(res)]
            assert "dead_func" in names
            assert "god_func" in names
            assert "helper1" not in names

            # 3. Test Lines threshold: lines > 50
            res = query(client, "GET functions WHERE lines > 50")
            assert res["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(res)]
            assert names == ["god_func"]

            # 4. Test combined numeric filtering: calls_count == 0 AND lines_count <= 15
            res = query(client, "GET functions WHERE calls_count == 0 AND lines_count <= 15")
            assert res["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(res)]
            assert "dead_func" in names
            assert "helper1" in names
            assert "helper2" in names
            assert "god_func" not in names
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_boolean_flags(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:async_fetch", "Function", "async_fetch", "a.py", is_async=True, is_generator=False)
            client.add_node("b.py:gen_stream", "Function", "gen_stream", "b.py", is_async=False, is_generator=True)
            client.add_node("c.py:regular", "Function", "regular", "c.py", is_async=False, is_generator=False)
            client.build()

            # 1. Query is_async == true
            res = query(client, "GET functions WHERE is_async == true")
            assert res["meta"]["ok"] is True
            assert [r["name"] for r in _flatten_results(res)] == ["async_fetch"]

            # 2. Query is_generator == true
            res = query(client, "GET functions WHERE is_generator == true")
            assert res["meta"]["ok"] is True
            assert [r["name"] for r in _flatten_results(res)] == ["gen_stream"]

            # 3. Query is_async == false
            res = query(client, "GET functions WHERE is_async == false")
            assert res["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(res)]
            assert "gen_stream" in names
            assert "regular" in names
            assert "async_fetch" not in names
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_param_count(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:simple", "Function", "simple", "a.py", param_count=1)
            client.add_node("b.py:complex_func", "Function", "complex_func", "b.py", param_count=7)
            client.add_node("c.py:medium", "Function", "medium", "c.py", param_count=3)
            client.build()

            # 1. Long parameter list query: param_count >= 5
            res = query(client, "GET functions WHERE param_count >= 5")
            assert res["meta"]["ok"] is True
            assert [r["name"] for r in _flatten_results(res)] == ["complex_func"]

            # 2. Alias query: args > 2
            res = query(client, "GET functions WHERE args > 2")
            assert res["meta"]["ok"] is True
            names = [r["name"] for r in _flatten_results(res)]
            assert "complex_func" in names
            assert "medium" in names
            assert "simple" not in names
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_is_exported(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:public_api", "Function", "public_api", "a.py", is_exported=True)
            client.add_node("a.py:_private_helper", "Function", "_private_helper", "a.py", is_exported=False)
            client.build()

            # 1. Query is_exported == true
            res = query(client, "GET functions WHERE is_exported == true")
            assert res["meta"]["ok"] is True
            assert [r["name"] for r in _flatten_results(res)] == ["public_api"]

            # 2. Query is_public == false
            res = query(client, "GET functions WHERE is_public == false")
            assert res["meta"]["ok"] is True
            assert [r["name"] for r in _flatten_results(res)] == ["_private_helper"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_without_where_clause(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py", "File", "a.py", "a.py")
            client.add_node("b.py", "File", "b.py", "b.py")
            client.build()

            # GET files LIMIT 20 (no WHERE clause)
            res = query(client, "GET files LIMIT 20")
            assert res["meta"]["ok"] is True
            assert len(_flatten_results(res)) == 2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_blast_radius_score(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:c", "Function", "c", "a.py")
            client.add_node("a.py:b", "Function", "b", "a.py", calls=["c"])
            client.add_node("a.py:a", "Function", "a", "a.py", calls=["b"])
            client.resolve_and_connect_calls("a.py:b", ["c"])
            client.resolve_and_connect_calls("a.py:a", ["b"])
            client.build()

            # c has callers [b] and transitive callers [b, a] -> blast_radius_score == 2
            res = query(client, "GET functions WHERE blast_radius_score >= 2")
            assert res["meta"]["ok"] is True
            items = _flatten_results(res)
            assert len(items) == 1
            assert items[0]["name"] == "c"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_query_not_regex(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:auth_login", "Function", "auth_login", "a.py")
            client.add_node("a.py:process_payment", "Function", "process_payment", "a.py")
            client.build()

            # Query !=~ '^(auth_|login_)'
            res = query(client, "GET functions WHERE name !=~ '^(auth_|login_)'")
            assert res["meta"]["ok"] is True
            items = _flatten_results(res)
            assert len(items) == 1
            assert items[0]["name"] == "process_payment"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_group_by_file_path(self):
        """GROUP BY file_path produces key-by-file_path grouping (default)."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            # File node for a.py
            client.add_node("a.py", "File", "a.py", "a.py", lines={"start": 1, "end": 50})
            # Functions in a.py
            client.add_node("a.py:foo", "Function", "foo", "a.py", lines={"start": 5, "end": 15})
            client.add_node("a.py:bar", "Function", "bar", "a.py", lines={"start": 20, "end": 30})
            # File node for b.py
            client.add_node("b.py", "File", "b.py", "b.py", lines={"start": 1, "end": 40})
            # Function in b.py
            client.add_node("b.py:baz", "Function", "baz", "b.py", lines={"start": 10, "end": 25})
            client.build()

            res = query(client, "GET * GROUP BY file_path")
            assert res["meta"]["ok"] is True
            assert res["meta"]["type"] == "all"
            grouped = res["results"]
            assert "a.py" in grouped
            assert "b.py" in grouped
            # a.py has file scalar + 2 functions
            a_entries = grouped["a.py"]
            assert isinstance(a_entries, list)
            assert len(a_entries) == 3
            # b.py has file scalar + 1 function
            b_entries = grouped["b.py"]
            assert isinstance(b_entries, list)
            assert len(b_entries) == 2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_group_by_type(self):
        """GROUP BY type groups by node type."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py", "File", "a.py", "a.py", lines={"start": 1, "end": 50})
            client.add_node("a.py:foo", "Function", "foo", "a.py", lines={"start": 5, "end": 15})
            client.add_node("a.py:FooClass", "Class", "FooClass", "a.py", lines={"start": 3, "end": 48})
            client.build()

            res = query(client, "GET * GROUP BY type")
            assert res["meta"]["ok"] is True
            grouped = res["results"]
            assert "file" in grouped
            assert "function" in grouped
            assert "class" in grouped
            # Each group holds compact entries (bare line-span for files, Name: start-end for symbols)
            assert grouped["file"] == "1-50"
            assert grouped["function"] == ["foo: 5-15"]
            assert grouped["class"] == ["FooClass: 3-48"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_group_by_folder(self):
        """GROUP BY folder groups by the first directory of file_path."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            # Files in different folders
            client.add_node("src/a.py", "File", "a.py", "src/a.py", lines={"start": 1, "end": 20})
            client.add_node("src/a.py:func1", "Function", "func1", "src/a.py", lines={"start": 5, "end": 15})
            client.add_node("tests/test_a.py", "File", "test_a.py", "tests/test_a.py", lines={"start": 1, "end": 30})
            client.add_node("tests/test_a.py:test_func1", "Function", "test_func1", "tests/test_a.py", lines={"start": 5, "end": 25})
            client.build()

            res = query(client, "GET * GROUP BY folder")
            assert res["meta"]["ok"] is True
            grouped = res["results"]
            assert "src" in grouped
            assert "tests" in grouped
            # Each folder has 2 entries (1 file scalar + 1 function)
            src_entries = grouped["src"] if isinstance(grouped["src"], list) else [grouped["src"]]
            tests_entries = grouped["tests"] if isinstance(grouped["tests"], list) else [grouped["tests"]]
            assert len(src_entries) == 2
            assert len(tests_entries) == 2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_aggregation_count_star(self):
        """COUNT(*) without GROUP BY returns total count."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py")
            client.add_node("a.py:bar", "Function", "bar", "a.py")
            client.add_node("b.py:baz", "Function", "baz", "b.py")
            client.build()

            res = query(client, "GET COUNT(*) functions")
            assert res["meta"]["ok"] is True
            assert res["meta"]["count"] == 3
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_aggregation_count_star_group_by(self):
        """COUNT(*) GROUP BY file_path returns per-file counts."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py")
            client.add_node("a.py:bar", "Function", "bar", "a.py")
            client.add_node("b.py:baz", "Function", "baz", "b.py")
            client.build()

            res = query(client, "GET COUNT(*) GROUP BY file_path")
            assert res["meta"]["ok"] is True
            assert res["meta"]["aggregation"] is True
            assert res["meta"]["group_by"] == "file_path"
            assert res["results"]["a.py"]["COUNT(*)"] == 2
            assert res["results"]["b.py"]["COUNT(*)"] == 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_aggregation_sum_lines(self):
        """SUM(lines_count) computes total lines."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py", lines={"start": 1, "end": 10})
            client.add_node("a.py:bar", "Function", "bar", "a.py", lines={"start": 11, "end": 30})
            client.add_node("b.py:baz", "Function", "baz", "b.py", lines={"start": 5, "end": 15})
            client.build()

            res = query(client, "GET SUM(lines_count)")
            assert res["meta"]["ok"] is True
            assert res["meta"]["aggregation"] is True
            assert res["results"]["SUM(lines_count)"] == 10 + 20 + 11
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_aggregation_avg_min_max(self):
        """AVG, MIN, MAX work correctly."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:f1", "Function", "f1", "a.py", lines={"start": 1, "end": 10})
            client.add_node("a.py:f2", "Function", "f2", "a.py", lines={"start": 11, "end": 40})
            client.add_node("a.py:f3", "Function", "f3", "a.py", lines={"start": 41, "end": 60})
            client.build()

            res = query(client, "GET AVG(lines_count), MIN(lines_count), MAX(lines_count)")
            assert res["meta"]["ok"] is True
            assert res["results"]["AVG(lines_count)"] == (10 + 30 + 20) / 3
            assert res["results"]["MIN(lines_count)"] == 10
            assert res["results"]["MAX(lines_count)"] == 30
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_aggregation_group_by_type(self):
        """Aggregation with GROUP BY type works."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py", "File", "a.py", "a.py", lines={"start": 1, "end": 50})
            client.add_node("a.py:foo", "Function", "foo", "a.py", lines={"start": 5, "end": 15})
            client.add_node("a.py:MyClass", "Class", "MyClass", "a.py", lines={"start": 3, "end": 45})
            client.build()

            res = query(client, "GET COUNT(*) GROUP BY type")
            assert res["meta"]["ok"] is True
            assert res["meta"]["aggregation"] is True
            assert res["meta"]["group_by"] == "type"
            assert res["results"]["function"]["COUNT(*)"] == 1
            assert res["results"]["class"]["COUNT(*)"] == 1
            assert res["results"]["file"]["COUNT(*)"] == 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_aggregation_mixed_fields(self):
        """Mixing aggregation functions with plain field names works (first val per group)."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py", lines={"start": 1, "end": 10})
            client.add_node("a.py:bar", "Function", "bar", "a.py", lines={"start": 11, "end": 30})
            client.build()

            # Mixed agg + plain field without GROUP BY → single result, first name
            res = query(client, "GET SUM(lines_count), name")
            assert res["meta"]["ok"] is True
            assert res["meta"]["aggregation"] is True
            assert res["results"]["SUM(lines_count)"] == 10 + 20
            assert res["results"]["name"] in ("foo", "bar")

            # Mixed agg + plain field WITH GROUP BY → per-group result
            client.add_node("b.py:baz", "Function", "baz", "b.py", lines={"start": 5, "end": 15})
            client.build()
            res2 = query(client, "GET SUM(lines_count), name GROUP BY file_path")
            assert res2["meta"]["ok"] is True
            assert res2["results"]["a.py"]["SUM(lines_count)"] == 10 + 20
            assert res2["results"]["a.py"]["name"] in ("foo", "bar")
            assert res2["results"]["b.py"]["SUM(lines_count)"] == 11
            assert res2["results"]["b.py"]["name"] == "baz"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_group_by_invalid_field_error(self):
        """Unrecognized GROUP BY fields raise ValueError."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py")
            client.build()

            res1 = query(client, "GET functions GROUP BY nonexistent")
            assert res1["ok"] is False
            assert "Invalid GROUP BY field" in res1["error"]

            res2 = query(client, "GET functions GROUP BY d")
            assert res2["ok"] is False
            assert "Invalid GROUP BY field" in res2["error"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_default_grouping(self):
        """GET without GROUP BY defaults to file_path grouping (backward compat)."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("a.py:foo", "Function", "foo", "a.py", lines={"start": 5, "end": 15})
            client.add_node("b.py:bar", "Function", "bar", "b.py", lines={"start": 10, "end": 25})
            client.build()

            # Without GROUP BY
            res = query(client, "GET functions")
            assert res["meta"]["ok"] is True
            grouped = res["results"]
            assert "a.py" in grouped
            assert "b.py" in grouped

            # With explicit GROUP BY file_path
            res2 = query(client, "GET functions GROUP BY file_path")
            assert res2["meta"]["ok"] is True
            assert res2["results"] == grouped
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestP0RegressionFixes:
    """P0 fixes: CHECK LAYERS local imports, GET result cap, METADATA callers."""

    def test_check_layers_detects_local_imports_with_file_paths(self):
        """File-path layer refs ('sales/services.py' AGAINST 'comptabilite/services.py')
        must match module-style imports, including in-function imports."""
        from src.query import query
        from src.watcher.sync_handler import GraphSyncHandler
        from src.database import get_graph_db, _db_instances
        import os
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            for d in ("sales", "comptabilite"):
                os.makedirs(os.path.join(tmpdir, d))
                open(os.path.join(tmpdir, d, "__init__.py"), "w").write("")
            open(os.path.join(tmpdir, "comptabilite", "services.py"), "w").write(
                "def generate_ecriture_vente():\n    return 'ok'\n")
            open(os.path.join(tmpdir, "sales", "services.py"), "w").write(
                "def complete_sale():\n"
                "    from comptabilite.services import generate_ecriture_vente\n"
                "    return generate_ecriture_vente()\n")

            key = os.path.abspath(tmpdir)
            _db_instances.pop(key, None)
            handler = GraphSyncHandler(tmpdir)
            db = get_graph_db(tmpdir)
            for root, _dirs, files in os.walk(tmpdir):
                for f in files:
                    if f.endswith(handler.supported_extensions):
                        p = os.path.join(root, f)
                        data = handler.parser.parse_file(p)
                        handler.update_file_in_graph(p, skip_rebuild=True, pre_parsed_data=data)
            db.client.repopulate_edges()
            db.client.resolve_api_calls()
            db.client.build()

            res = query(db.client, "CHECK LAYERS 'sales/services.py' AGAINST 'comptabilite/services.py'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["clean"] is False
            assert res["meta"]["total"] >= 1
            assert res["results"][0]["file"] == "sales/services.py"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_check_layers_file_ref_no_false_positive_inside_layer(self):
        """Imports within the same file/layer stay internal (no violation)."""
        from src.query.compiler import _import_violates_layer
        assert _import_violates_layer("comptabilite.services", "comptabilite/services.py", "sales/services.py") is True
        assert _import_violates_layer("sales.helpers", "comptabilite/services.py", "sales/services.py") is False
        assert _import_violates_layer("comptabilite.services", "comptabilite/services", "") is True
        assert _import_violates_layer("pos_caisse.domain.entities", "comptabilite/services.py", "pos_caisse.domain") is False

    def test_get_hard_result_cap(self):
        """Explicit LIMIT is honored up to MAX_QUERY_RESULTS; LIMIT ALL bypasses the cap."""
        from src.query import query
        from src.query.compiler import MAX_QUERY_RESULTS
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            for i in range(1100):
                client.add_node(f"a.py:func{i}", "Function", f"func{i}", "a.py")
            client.build()

            # A careless huge LIMIT is still capped at MAX_QUERY_RESULTS
            res = query(client, "GET functions LIMIT 5000")
            assert res["meta"]["ok"] is True
            assert res["meta"]["count"] == MAX_QUERY_RESULTS
            assert res["meta"]["total"] == 1100
            assert res["meta"]["truncated"] is True
            # Pagination hint still available
            assert "hint" in res["meta"]

            # Explicit LIMIT ALL fetches everything in one query
            res_all = query(client, "GET functions LIMIT ALL")
            assert res_all["meta"]["ok"] is True
            assert res_all["meta"]["count"] == 1100
            assert res_all["meta"]["truncated"] is False
            assert "hint" not in res_all["meta"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_metadata_callers_full_and_deduplicated(self):
        """METADATA direct_callers must be complete (no silent truncation) and unique."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("target.py:work", "Function", "work", "target.py")
            for i in range(25):
                client.add_node(f"c{i}.py:caller", "Function", f"caller{i}", f"c{i}.py")
                # Duplicate edges simulate duplicate call sites in the CSR graph
                client.add_edge(f"c{i}.py:caller", "target.py:work")
                client.add_edge(f"c{i}.py:caller", "target.py:work")
            client.build()

            res = query(client, "METADATA FOR 'target.py:work'")
            callers = res["results"]["direct_callers"]
            assert res["meta"]["callers_count"] == 25
            assert len(callers) == 25
            assert len(callers) == len(set(callers))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestP1RegressionFixes:
    """P1 fixes: stale index detection, literal boolean predicates, type-first agg syntax."""

    def _build_client(self, tmpdir, n=5):
        from src.database.graph_client import EngramClient
        client = EngramClient(tmpdir)
        for i in range(n):
            client.add_node(f"m{i}.py:gen{i}", "Function", f"gen{i}", f"m{i}.py",
                            lines={"start": 1, "end": 10 + i})
        client.build()
        return client

    def test_literal_boolean_predicates(self):
        """WHERE 1==1 / 0==1 / true / false / NOT true must evaluate as constants."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build_client(tmpdir)
            assert query(client, "GET functions WHERE 1==1 LIMIT 3")["meta"]["count"] == 3
            assert query(client, "GET functions WHERE 0==1 LIMIT 3")["meta"]["count"] == 0
            assert query(client, "GET functions WHERE 1!=2 LIMIT 3")["meta"]["count"] == 3
            assert query(client, "GET functions WHERE true LIMIT 3")["meta"]["count"] == 3
            assert query(client, "GET functions WHERE false LIMIT 3")["meta"]["count"] == 0
            assert query(client, "GET functions WHERE NOT true LIMIT 3")["meta"]["count"] == 0
            assert query(client, "GET functions WHERE 1!=2 AND name LIKE 'gen*' LIMIT 3")["meta"]["count"] == 3
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_literal_predicate_invalid_parse_not_crash(self):
        """Malformed constant expressions must error cleanly, not 500."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build_client(tmpdir)
            res = query(client, "GET functions WHERE == 1")
            assert res["ok"] is False
            assert "Parse error" in res["error"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_type_first_aggregation(self):
        """Type-first aggregation syntax: GET functions SUM(lines_count)."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build_client(tmpdir, n=3)
            res = query(client, "GET functions SUM(lines_count)")
            assert res["meta"]["ok"] is True
            assert res["meta"]["aggregation"] is True
            assert res["results"]["SUM(lines_count)"] == 33.0  # 10+11+12
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_group_by_arbitrary_field(self):
        """GROUP BY must accept any metadata field present in results."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build_client(tmpdir, n=5)
            res = query(client, "GET functions GROUP BY name")
            assert res["meta"]["ok"] is True
            assert res["meta"]["count"] == 5
            keys = list(res["results"].keys())
            assert keys == [f"gen{i}" for i in range(5)]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_group_by_unknown_field_errors(self):
        """GROUP BY a field that appears nowhere must error, not silently pass."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build_client(tmpdir)
            res = query(client, "GET functions GROUP BY not_a_real_field")
            assert res["ok"] is False
            assert "Invalid GROUP BY field" in res["error"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_index_stale_flag_fresh(self):
        """A freshly written index marker reports index_stale=False."""
        from src.query import query
        from src.database.graph_client import INDEX_META_FILENAME
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build_client(tmpdir)
            import os
            client.write_index_meta()
            assert os.path.exists(os.path.join(tmpdir, INDEX_META_FILENAME))
            res = query(client, "GET functions LIMIT 1")
            assert res["meta"]["ok"] is True
            assert res["meta"]["index_stale"] is False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_index_stale_flag_detects_mutated_index(self):
        """Rewriting the sidecar fingerprint must flag the index as stale."""
        from src.query import query
        from src.database.graph_client import INDEX_META_FILENAME
        from src.database.parser.language_adapter import compute_index_fingerprint
        import tempfile
        import os
        import json
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build_client(tmpdir)
            client.write_index_meta()
            meta_path = os.path.join(tmpdir, INDEX_META_FILENAME)
            with open(meta_path) as f:
                meta = json.load(f)
            assert meta["fingerprint"] == compute_index_fingerprint()

            meta["fingerprint"] = "0000deadbeef0000"
            with open(meta_path, "w") as f:
                json.dump(meta, f)

            res = query(client, "GET functions LIMIT 1")
            assert res["meta"]["ok"] is True
            assert res["meta"]["index_stale"] is True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)



class TestP2UsabilityFixes:
    """P2 fixes: SEARCH ranking, GLOB path-only, route search, pagination,
    external symbols, STATS depth, FLOW/METADATA dedup consistency."""

    def _build(self, tmpdir):
        from src.database.graph_client import EngramClient
        client = EngramClient(tmpdir)
        client.add_node("svc.py", "File", "svc.py", "svc.py", lines={"start": 1, "end": 20})
        client.add_node("svc.py:create", "Function", "create", "svc.py",
                        signature="def create()", lines={"start": 1, "end": 3})
        client.add_node("svc.py:helper", "Function", "helper", "svc.py",
                        signature="def helper():\n    return create()", lines={"start": 4, "end": 7})
        client.add_node("svc.py:create_wrapper", "Function", "create_wrapper", "svc.py",
                        signature="def create_wrapper():\n    return create()", lines={"start": 8, "end": 10})
        client.add_node("api.py:/api/sales", "Route", "/api/sales", "api.py",
                        lines={"start": 1, "end": 5},
                        _extra={"view_name": "add_item", "url": "/api/sales", "methods": ["POST"]})
        client.build()
        return client

    def test_search_ranks_name_matches_first(self):
        """SEARCH 'create' must float exact/prefix name matches above body-only hits."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            res = query(client, "SEARCH 'create' LIMIT 10")
            assert res["meta"]["ok"] is True
            assert res["meta"]["total"] == 3
            names = [r["name"] for r in _flatten_results(res)]
            assert names[0] == "create"          # exact name match first
            assert names[1] == "create_wrapper"  # prefix name match second
            assert "note" in res["meta"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_glob_returns_files_only(self):
        """GLOB matches File/Folder paths only — symbols in matched files excluded."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            res = query(client, "GLOB '*svc.py'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["total"] == 1
            # Compact: {file_path: line-span} — file path is the sole result key
            assert list(res["results"].keys()) == ["svc.py"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_search_routes_by_view_name(self):
        """SEARCH IN routes must match view_name/url/methods, not just URL-as-name."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            res = query(client, "SEARCH 'add_item' IN routes LIMIT 10")
            assert res["meta"]["ok"] is True
            assert res["meta"]["total"] == 1
            # Route found via view_name, compact entry renders as full_url: start-end
            assert _flatten_results(res)[0]["name"] == "/api/sales"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_routes_direct_query(self):
        """GET * FROM routes must return Route nodes (case-insensitive type match)."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            res = query(client, "GET * FROM routes")
            assert res["meta"]["ok"] is True
            assert res["meta"]["type"] == "route"
            assert res["meta"]["total"] == 1
            flat = _flatten_results(res)
            assert flat[0]["name"] == "/api/sales"
            assert flat[0]["type"] == "Route"
            assert flat[0]["view_name"] == "add_item"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_search_in_star_searches_all_types(self):
        """SEARCH '...' IN * / IN ALL must search all types — the explicit scope
        must NOT be narrowed to files by the filename heuristic."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            for q in ('SEARCH "Effect.gen" IN *',
                      'SEARCH "Effect.gen" IN ALL'):
                res = query(client, q)
                assert res["meta"]["ok"] is True, f"failed: {q}"
                assert res["meta"]["type"] == "all", f"{q} -> {res['meta'].get('type')}"
            # Bare filename search (no IN) still narrows to the File node
            res = query(client, 'SEARCH "svc.py"')
            assert res["meta"]["type"] == "file"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_search_in_star_parser_scope(self):
        """Parser must record scope='all' for explicit IN *, and None for bare search."""
        from src.query.parser import parse_query
        assert parse_query('SEARCH "x" IN *').scope == "all"
        assert parse_query('SEARCH "x" IN ALL').scope == "all"
        assert parse_query('SEARCH "x" IN routes').scope == "route"
        assert parse_query('SEARCH "x"').scope is None

    def test_find_decorated_default_paginates(self):
        """FIND DECORATED must default to the unified page size, not return everything."""
        from src.query import query
        from src.query.compiler import DEFAULT_PAGE_SIZE
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            for i in range(DEFAULT_PAGE_SIZE + 10):
                client.add_node(f"m.py:D{i}", "Class", f"D{i}", "m.py",
                                _extra={"decorators": ["@dataclass"]},
                                lines={"start": 1, "end": 2})
            client.build()
            res = query(client, "FIND DECORATED WITH 'dataclass'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["count"] == DEFAULT_PAGE_SIZE
            assert res["meta"]["total"] == DEFAULT_PAGE_SIZE + 10
            assert res["meta"]["truncated"] is True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_metadata_external_callees_classified(self):
        """Unresolved call targets (Decimal, ORM chains) are flagged as external dead ends."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("svc.py:process", "Function", "process", "svc.py",
                            calls=["Decimal", "ValidationError", "Ledger.objects.create", "internal_helper"],
                            lines={"start": 1, "end": 5})
            client.add_node("svc.py:internal_helper", "Function", "internal_helper", "svc.py",
                            lines={"start": 1, "end": 3})
            client.build()

            res = query(client, "METADATA FOR 'svc.py:process'")
            ext = res["results"]["external_callees"]
            kinds = {e["name"]: e["kind"] for e in ext}
            assert kinds["Decimal"] == "stdlib"
            assert kinds["ValidationError"] == "third_party"
            assert kinds["Ledger.objects.create"] == "attribute_chain"
            assert "internal_helper" not in kinds  # resolved in-graph call
            assert res["meta"]["external_callees_count"] == 3
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_stats_reports_extra_loc_and_coverage(self):
        """STATS includes api/selector/schema LOC and a test-coverage ratio."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/modules/sales/services.py", "File", "services.py", "src/modules/sales/services.py",
                            lines={"start": 1, "end": 50})
            client.add_node("src/modules/sales/api.py", "File", "api.py", "src/modules/sales/api.py",
                            lines={"start": 1, "end": 30})
            client.add_node("src/modules/sales/selectors.py", "File", "selectors.py", "src/modules/sales/selectors.py",
                            lines={"start": 1, "end": 20})
            client.add_node("src/modules/sales/schemas.py", "File", "schemas.py", "src/modules/sales/schemas.py",
                            lines={"start": 1, "end": 40})
            client.add_node("src/modules/sales/tests.py", "File", "tests.py", "src/modules/sales/tests.py",
                            lines={"start": 1, "end": 25})
            client.build()

            res = query(client, "STATS FOR 'src/modules/sales'")
            mod = res["results"]["modules"][0]
            assert mod["services_loc"] == 50
            assert mod["api_loc"] == 30
            assert mod["selector_loc"] == 20
            assert mod["schema_loc"] == 40
            assert mod["test_files"] == 1
            assert res["meta"]["test_file_ratio"] == 0.2  # 1 test / 5 files
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_stats_root_files_are_not_modules(self):
        """STATS must not list bare root-level files (README.md, requirements.txt,
        *.json) as modules — only real directories are modules."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("README.md", "File", "README.md", "README.md", lines={"start": 1, "end": 10})
            client.add_node("requirements.txt", "File", "requirements.txt", "requirements.txt", lines={"start": 1, "end": 5})
            client.add_node(".cordyceps_index_meta.json", "File", ".cordyceps_index_meta.json",
                            ".cordyceps_index_meta.json", lines={"start": 1, "end": 3})
            client.add_node("src/modules/sales/services.py", "File", "services.py", "src/modules/sales/services.py",
                            lines={"start": 1, "end": 50})
            client.add_node("src/watcher/sync_handler.py", "File", "sync_handler.py", "src/watcher/sync_handler.py",
                            lines={"start": 1, "end": 30})
            client.build()

            res = query(client, "STATS FOR '.'")
            mods = res["results"]["modules"]
            names = [m["name"] for m in mods]
            assert "sales" in names
            assert "watcher" in names
            assert not any(m in names for m in ("README.md", "requirements.txt", ".cordyceps_index_meta.json"))
            # Root junk files still count toward the totals, just not as modules
            assert res["meta"]["files"] == 5
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_stats_defaults_to_workspace_and_accepts_absolute_paths(self):
        """STATS is usable without a path and accepts the agent's absolute path."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil
        import os

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/a.py", "File", "a.py", "src/a.py", lines={"start": 1, "end": 10})
            client.add_node("src2/b.py", "File", "b.py", "src2/b.py", lines={"start": 1, "end": 20})
            client.build()

            root = query(client, "STATS")
            assert root["meta"]["files"] == 2

            relative = query(client, "STATS FOR 'src'")
            assert relative["meta"]["files"] == 1

            absolute = query(client, f'STATS FOR "{os.path.join(tmpdir, "src")}"')
            assert absolute["meta"]["files"] == 1
            assert absolute["meta"]["path"] == "src"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_flow_reports_duplicates_traced(self):
        """FLOW output surfaces the dedup behavior instead of only a cosmetic marker."""
        from unittest import mock
        from src.query import query
        from src.database import get_graph_db, _db_instances
        import tempfile
        import shutil
        import os

        tmpdir = tempfile.mkdtemp()
        try:
            db = get_graph_db(tmpdir)
            db.client.add_node("src/modules/sales/services.py:complete_sale", "Function", "complete_sale",
                               "src/modules/sales/services.py", lines={"start": 1, "end": 5})
            db.client.add_node("src/modules/sales/services.py:generate_invoice", "Function", "generate_invoice",
                               "src/modules/sales/services.py", lines={"start": 1, "end": 5})
            db.client.add_node("src/modules/core/services.py:log_event", "Function", "log_event",
                               "src/modules/core/services.py", lines={"start": 1, "end": 5})
            db.client.add_edge("src/modules/sales/services.py:complete_sale", "src/modules/sales/services.py:generate_invoice")
            db.client.add_edge("src/modules/sales/services.py:generate_invoice", "src/modules/core/services.py:log_event")
            db.client.add_edge("src/modules/sales/services.py:complete_sale", "src/modules/core/services.py:log_event")
            db.client.build()

            with mock.patch("src.database.get_graph_db", return_value=db):
                res = query(db.client, "FLOW FOR 'src/modules/sales/services.py:complete_sale'")
            assert res["meta"]["ok"] is True
            assert "duplicates_traced" in res["meta"]
        finally:
            _db_instances.pop(os.path.abspath(tmpdir), None)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_flow_through_parses_and_dispatches_to_pipeline(self):
        """FLOW THROUGH must parse in all documented shapes and compile to the
        middleware-pipeline query (regression: THROUGH_KW was unexpected)."""
        from unittest import mock
        from src.query import query
        from src.query.parser import parse_query
        from src.database import get_graph_db, _db_instances
        import tempfile
        import shutil
        import os

        # 1) Parser accepts every THROUGH shape.
        for qs in (
            "FLOW THROUGH '/rentals/:id/return'",
            "FLOW FOR '/rentals/:id/return' THROUGH route middleware handler",
            "FLOW FROM '/rentals/:id/return' THROUGH middleware",
        ):
            parsed = parse_query(qs)
            assert parsed.route_url == "/rentals/:id/return", qs

        tmpdir = tempfile.mkdtemp()
        try:
            db = get_graph_db(tmpdir)
            db.client.add_node(
                "api/routes.ts:/rentals/:id/return", "Route", "/rentals/:id/return",
                "api/routes.ts",
                _extra={"view_name": "handler_post_L4", "url": "/rentals/:id/return",
                        "func": "add_router", "source_var": "router"},
            )
            db.client.add_node(
                "api/routes.ts:handler_post_L4", "Function", "handler_post_L4",
                "api/routes.ts", lines={"start": 4, "end": 8}, calls=["db.update"],
            )
            db.client.add_node(
                "api/routes.ts:mw:auth", "Middleware", "auth", "api/routes.ts",
                _extra={"source_var": "MIDDLEWARE", "middleware_type": "auth"},
            )
            db.client.add_edge("api/routes.ts:/rentals/:id/return", "api/routes.ts:handler_post_L4")
            db.client.build()

            with mock.patch("src.database.get_graph_db", return_value=db):
                res = query(db.client, "FLOW THROUGH '/rentals/:id/return'")
            assert res["meta"]["query_type"] == "FLOW_PIPELINE"
            assert res["meta"]["resolution"]["route"] == "resolved"
            assert res["meta"]["resolution"]["handler"] == "resolved"
            kinds = [s["kind"] for s in res["results"]["pipeline"]]
            assert "handler" in kinds
            assert "middleware" in kinds
        finally:
            _db_instances.pop(os.path.abspath(tmpdir), None)
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestBugfixBatch:
    """Regression tests for the latest bug batch: ENFORCE direction/chains,
    MUST_BE path scope, glob layer expansion, LAYERS OF from-import handling,
    GLOB extglob negation, and STATS test-file/test-function consistency."""

    def _build(self, tmpdir):
        from src.database.graph_client import EngramClient
        client = EngramClient(tmpdir)
        client.add_node("src/modules/sales/api.py", "File", "api.py", "src/modules/sales/api.py",
                        lines={"start": 1, "end": 20},
                        _extra={"imports": ["from src.modules.sales import selectors, services",
                                            "from django.db import transaction"]})
        client.add_node("src/modules/sales/services.py", "File", "services.py", "src/modules/sales/services.py",
                        lines={"start": 1, "end": 20})
        client.add_node("src/modules/sales/models.py", "File", "models.py", "src/modules/sales/models.py",
                        lines={"start": 1, "end": 20})
        client.add_node("src/modules/catalog/api.py", "File", "api.py", "src/modules/catalog/api.py",
                        lines={"start": 1, "end": 20},
                        _extra={"imports": ["src.modules.catalog.services"]})
        client.add_node("src/modules/catalog/services.py", "File", "services.py", "src/modules/catalog/services.py",
                        lines={"start": 1, "end": 20})
        client.build()
        return client

    def test_enforce_direction_detects_multi_symbol_from_import(self):
        """'from src.modules.sales import selectors, services' must count as a
        dependency on services.py — the chain rule must VIOLATE, not false-pass."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            res = query(client, "ENFORCE 'src/modules/sales/services.py <- src/modules/sales/api.py'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "VIOLATED"
            assert res["meta"]["count"] == 1
            assert res["meta"]["files_checked"] >= 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_enforce_chain_validates_all_pairs(self):
        """Three-layer chain checks both (b→a) and (c→a/c→b) directions."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            res = query(client, "ENFORCE 'src/modules/sales/services.py <- src/modules/sales/models.py <- src/modules/sales/api.py'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "VIOLATED"
            assert res["meta"]["count"] == 1
            assert res["meta"]["files_checked"] == 3
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_enforce_glob_layers_expand_and_count(self):
        """Glob layer refs must expand to real files so files_checked > 0 and
        violations are actually found (previously a silent false-pass)."""
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = self._build(tmpdir)
            res = query(client, "ENFORCE \"src/modules/*/api.py MUST_NOT_IMPORT src/modules/*/services.py\"")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "VIOLATED"
            assert res["meta"]["files_checked"] == 2
            assert res["meta"]["count"] >= 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_enforce_must_be_scoped_to_path(self):
        """MUST_BE decorated_with on a bare file path must scope entities_checked
        to that file and keep the path in the rule label."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/modules/sales/services.py:create_order", "Function", "create_order",
                            "src/modules/sales/services.py", _extra={"decorators": ["transaction.atomic"]},
                            lines={"start": 1, "end": 5})
            client.add_node("src/modules/sales/services.py:helper", "Function", "helper",
                            "src/modules/sales/services.py", lines={"start": 1, "end": 5})
            client.add_node("src/modules/other.py:thing", "Function", "thing",
                            "src/modules/other.py", lines={"start": 1, "end": 5})
            client.build()

            res = query(client, "ENFORCE \"src/modules/sales/services.py MUST_BE decorated_with 'transaction.atomic'\"")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "VIOLATED"
            assert res["meta"]["entities_checked"] == 2
            assert "src/modules/sales/services.py" in res["meta"]["rule"]
            assert "Classs" not in res["meta"]["rule"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_layers_of_classifies_from_import_as_project(self):
        """LAYERS OF must not count 'from src.modules.comptabilite import selectors'
        as third_party (previously inflated the third_party bucket)."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/modules/sales/api.py", "File", "api.py", "src/modules/sales/api.py",
                            lines={"start": 1, "end": 20},
                            _extra={"imports": ["from src.modules.comptabilite import selectors",
                                                "django.db.models"]})
            client.add_node("src/modules/comptabilite/selectors.py", "File", "selectors.py",
                            "src/modules/comptabilite/selectors.py", lines={"start": 1, "end": 20})
            client.build()

            res = query(client, "LAYERS OF 'src/modules/sales'")
            deps = res["results"]
            assert "from src.modules.comptabilite import selectors" in deps["project"]["imports"]
            assert "from src.modules.comptabilite import selectors" not in deps["third_party"]["imports"]
            assert deps["third_party"]["import_count"] == 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_layers_of_root_error_includes_valid_query_example(self):
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/modules/sales/api.py", "File", "api.py",
                            "src/modules/sales/api.py", lines={"start": 1, "end": 20})
            client.build()

            res = query(client, "LAYERS OF '.'")
            assert res["ok"] is False
            assert "Root is not a layer" in res["error"]
            assert 'Example: LAYERS OF "src/modules/sales"' in res["error"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_glob_extglob_negation(self):
        """GLOB !(comptabilite) must exclude that single segment (previously
        matched nothing because the lookahead anchored $ to end-of-string)."""
        from src.query import query
        from src.query.compiler import _glob_match
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            for mod in ("sales", "catalog", "comptabilite"):
                client.add_node(f"src/modules/{mod}/services.py", "File", "services.py",
                                f"src/modules/{mod}/services.py", lines={"start": 1, "end": 10})
            client.build()

            assert _glob_match("src/modules/sales/services.py", "src/modules/!(comptabilite)/services.py") is True
            assert _glob_match("src/modules/comptabilite/services.py", "src/modules/!(comptabilite)/services.py") is False

            res = query(client, "GLOB 'src/modules/!(comptabilite)/services.py'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["total"] == 2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_stats_test_counts_consistent(self):
        """STATS test_files > 0 must imply test_functions > 0 (previously
        test_functions could read 0 while test_files reported 1)."""
        from src.query import query
        from src.database.graph_client import EngramClient
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/modules/sales/tests.py", "File", "tests.py", "src/modules/sales/tests.py",
                            lines={"start": 1, "end": 30})
            client.add_node("src/modules/sales/tests.py:test_create_order", "Function", "test_create_order",
                            "src/modules/sales/tests.py", signature="def test_create_order():", lines={"start": 1, "end": 5})
            client.add_node("src/modules/sales/tests.py:test_invoice_total", "Function", "test_invoice_total",
                            "src/modules/sales/tests.py", signature="def test_invoice_total():", lines={"start": 1, "end": 5})
            client.build()

            res = query(client, "STATS FOR 'src/modules/sales'")
            tc = res["results"]["test_coverage"]
            assert tc["test_files"] == 1
            assert tc["test_functions"] >= 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestEnforceUncheckedVerdict:
    """ENFORCE must never report PASSED when zero files/entities were inspected
    (vacuous pass). A layer/scope that matches nothing yields UNCHECKED + reason."""

    def _seed(self, tmpdir):
        from src.database.graph_client import EngramClient
        client = EngramClient(tmpdir)
        client.add_node("src/modules/sales/services.py", "File", "services.py",
                        "src/modules/sales/services.py", lines={"start": 1, "end": 20})
        client.add_node("src/modules/sales/api.py", "File", "api.py",
                        "src/modules/sales/api.py", lines={"start": 1, "end": 20},
                        _extra={"imports": ["from django.db import transaction"]})
        client.build()
        return client

    def test_must_not_import_dedupes_mixed_import_forms(self):
        """Bare-module + full-statement entries of the SAME import must count once,
        render verbatim (no 'from from'), and carry real line numbers."""
        from src.database.graph_client import EngramClient
        from src.query import query
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            client = EngramClient(tmpdir)
            client.add_node("src/modules/billing/services.py", "File", "services.py",
                            "src/modules/billing/services.py", lines={"start": 1, "end": 20})
            client.add_node("src/modules/inventory/api.py", "File", "api.py",
                            "src/modules/inventory/api.py", lines={"start": 1, "end": 30},
                            _extra={
                                "imports": [
                                    "from src.modules.billing.services import ChargeCard",
                                    "src.modules.billing.services",
                                    "import src.modules.billing.audit",
                                    "src.modules.billing.audit",
                                    "import json",
                                ],
                                "import_lines": {
                                    "from src.modules.billing.services import ChargeCard": 5,
                                    "src.modules.billing.services": 5,
                                    "import src.modules.billing.audit": 6,
                                    "src.modules.billing.audit": 6,
                                    "import json": 2,
                                },
                            })
            client.build()

            res = query(client, "ENFORCE 'src/modules/inventory MUST_NOT_IMPORT src/modules/billing'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "VIOLATED"
            # 2 real statements -> exactly 2 violations (not 4 via bare duplicates)
            assert res["meta"]["count"] == 2, res["results"]
            statements = sorted(v["statement"] for v in res["results"])
            assert statements == [
                "from src.modules.billing.services import ChargeCard",
                "import src.modules.billing.audit",
            ]
            lines = sorted(v["line"] for v in res["results"])
            assert lines == [5, 6]
            assert all(v["line"] is not None for v in res["results"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_must_not_import_empty_layer_unchecked(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "ENFORCE 'src/modules/ghost MUST_NOT_IMPORT src/modules/sales'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "UNCHECKED"
            assert res["meta"]["files_checked"] == 0
            assert "matched 0" in res["meta"]["reason"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_must_not_import_glob_empty_layer_unchecked(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "ENFORCE 'src/modules/*/api.py MUST_NOT_IMPORT src/modules/nope/*.py'")
            assert res["meta"]["status"] == "UNCHECKED"
            assert "matched 0 files" in res["meta"]["reason"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_direction_chain_missing_layer_unchecked(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "ENFORCE 'src/modules/sales/services.py <- src/modules/ghost/api.py'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "UNCHECKED"
            assert res["meta"]["files_checked"] == 0
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_circular_empty_scope_unchecked(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "ENFORCE 'NO_CIRCULAR_DEPENDENCIES' IN 'src/modules/ghost'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "UNCHECKED"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_must_be_decorated_no_entities_unchecked(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, 'ENFORCE "src/modules/ghost/services.py MUST_BE decorated_with \'transaction.atomic\'"')
            assert res["meta"]["ok"] is True
            assert res["meta"]["status"] == "UNCHECKED"
            assert res["meta"]["entities_checked"] == 0
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_clean_rule_still_passes(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "ENFORCE 'src/modules/sales/api.py MUST_NOT_IMPORT src/modules/sales/services.py'")
            assert res["meta"]["status"] == "PASSED"
            assert res["meta"]["files_checked"] == 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestModulePackageQueries:
    """modules/packages as official GET/SEARCH entity targets."""

    def _seed(self, tmpdir):
        from src.database.graph_client import EngramClient
        client = EngramClient(tmpdir)
        client.add_node("src/modules/sales/services.py", "File", "services.py",
                        "src/modules/sales/services.py", lines={"start": 1, "end": 200})
        client.add_node("src/modules/sales/api.py", "File", "api.py",
                        "src/modules/sales/api.py", lines={"start": 1, "end": 100})
        client.add_node("src/modules/sales/services.py:create_sale", "Function", "create_sale",
                        "src/modules/sales/services.py")
        client.add_node("src/modules/catalog/__init__.py", "File", "__init__.py",
                        "src/modules/catalog/__init__.py", lines={"start": 1, "end": 1})
        client.add_node("src/modules/catalog/models.py", "File", "models.py",
                        "src/modules/catalog/models.py", lines={"start": 1, "end": 50})
        client.add_node("src/modules/catalog/models.py:Product", "Class", "Product",
                        "src/modules/catalog/models.py")
        client.add_node("engram_core/src/lib.rs", "File", "lib.rs",
                        "engram_core/src/lib.rs", lines={"start": 1, "end": 400})
        client.build()
        return client

    def test_get_modules(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "GET * FROM modules")
            assert res["meta"]["ok"] is True
            assert res["meta"]["type"] == "module"
            assert res["meta"]["total"] == 3  # sales, catalog, engram_core
            names = sorted(r["name"] for r in res["results"])
            assert names == ["catalog", "engram_core", "sales"]
            sales = next(r for r in res["results"] if r["name"] == "sales")
            assert sales["type"] == "Module"
            assert sales["file_path"] == "src/modules/sales"
            assert sales["files"] == 2
            assert sales["functions"] == 1
            assert sales["classes"] == 0
            assert sales["lines_count"] == 300
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_modules_count(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "GET COUNT(*) FROM modules")
            assert res["meta"]["ok"] is True
            assert res["meta"]["type"] == "module"
            assert res["meta"]["count"] == 3
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_modules_where_and_projection(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "GET name, files FROM modules WHERE name LIKE 'sales'")
            assert res["meta"]["ok"] is True
            assert res["meta"]["total"] == 1
            assert res["results"][0]["name"] == "sales"
            assert res["results"][0]["files"] == 2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_get_packages(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, "GET * FROM packages")
            assert res["meta"]["ok"] is True
            assert res["meta"]["type"] == "package"
            assert res["meta"]["total"] == 1  # only catalog has __init__.py
            pkg = res["results"][0]
            assert pkg["name"] == "catalog"
            assert pkg["type"] == "Package"
            assert pkg["files"] == 2
            assert pkg["classes"] == 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_search_in_modules(self):
        from src.query import query
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            client = self._seed(tmpdir)
            res = query(client, 'SEARCH "sales" IN modules')
            assert res["meta"]["ok"] is True
            assert res["meta"]["total"] == 1
            assert _flatten_results(res)[0]["name"] == "sales"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def test_python_contextual_call_resolution_end_to_end(isolated_temp_dir):
    import os
    from src.database import get_graph_db, _db_instances
    from src.watcher.sync_handler import GraphSyncHandler

    workspace = isolated_temp_dir
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "def work():\n    return 'a'\n",
        "pkg/b.py": "def work():\n    return 'b'\n",
        "pkg/repository.py": """
class Repository:
    def save(self):
        return True
""",
        "pkg/base.py": """
class ExternalBase:
    def execute(self):
        return True
""",
        "caller.py": """
from pkg.a import work as run
from pkg.a import work
import pkg.b as svc
import pkg.a
import pkg.b

def call_a():
    return run()

def call_b():
    return svc.work()

def call_unknown(obj):
    return obj.work()

def call_unimported():
    return remote_only()

def call_dotted_a():
    return pkg.a.work()

def call_dotted_b():
    return pkg.b.work()

def call_shadowed(work):
    return work()
""",
        "models.py": """
class A:
    def run(self):
        return self.save()

    def save(self):
        return 'a'

class B:
    def save(self):
        return 'b'

class Base:
    def execute(self):
        return 'base'

    def validate(self):
        return True

class Child(Base):
    def execute(self):
        return super().execute()

    def call_validate(self):
        return self.validate()
""",
        "nested.py": """
def target():
    return True

def parent():
    def child():
        return target()
    return child()
""",
        "other.py": "def target():\n    return False\n",
        "remote.py": "def remote_only():\n    return False\n",
        "typed.py": """
from pkg.repository import Repository as Repo
import pkg.base

repo = Repo()

class AlternateRepo:
    def save(self):
        return False

class BaseRepo:
    def save(self):
        return True

class ChildRepo(BaseRepo):
    pass

def persist(repo: Repo):
    return repo.save()

def construct():
    repo = Repo()
    return repo.save()

class Service:
    def __init__(self):
        self.repo = Repo()

    def persist(self):
        return self.repo.save()

class Container:
    repo: AlternateRepo

    def persist(self):
        return repo.save()

class StaticContainer:
    @staticmethod
    def persist(self: Repo):
        return self.save()

class ExternalChild(pkg.base.ExternalBase):
    def run(self):
        return super().execute()

def persist_child(repo: ChildRepo):
    return repo.save()

def persist_union(repo: Repo | AlternateRepo):
    return repo.save()

def outer(repo: Repo):
    def inner(repo):
        return repo.save()
    return inner(repo)
""",
    }
    for relative, source in files.items():
        path = os.path.join(workspace, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(source)

    key = os.path.abspath(workspace)
    _db_instances.pop(key, None)
    handler = GraphSyncHandler(workspace)
    db = get_graph_db(workspace)
    try:
        for relative in sorted(files):
            path = os.path.join(workspace, relative)
            parsed = handler.parser.parse_file(path)
            handler.update_file_in_graph(path, skip_rebuild=True, pre_parsed_data=parsed)
        db.client.repopulate_edges()
        db.client.build()

        def function_callees(node_id):
            return {
                callee for callee in db.client.get_callees(node_id)
                if (db.client.get_node_meta(callee) or {}).get("type") == "Function"
            }

        assert function_callees("caller.py:call_a") == {"pkg/a.py:work"}
        assert function_callees("caller.py:call_b") == {"pkg/b.py:work"}
        assert function_callees("caller.py:call_unknown") == set()
        assert function_callees("caller.py:call_unimported") == set()
        assert function_callees("caller.py:call_dotted_a") == {"pkg/a.py:work"}
        assert function_callees("caller.py:call_dotted_b") == {"pkg/b.py:work"}
        assert function_callees("caller.py:call_shadowed") == set()
        assert function_callees("models.py:A.run") == {"models.py:A.save"}
        assert function_callees("models.py:Child.execute") == {"models.py:Base.execute"}
        assert function_callees("models.py:Child.call_validate") == {"models.py:Base.validate"}
        assert function_callees("nested.py:parent") == {"nested.py:parent.child"}
        assert function_callees("nested.py:parent.child") == {"nested.py:target"}
        assert "nested.py:parent" in db.client.get_dependencies("nested.py:parent.child")
        assert function_callees("typed.py:persist") == {"pkg/repository.py:Repository.save"}
        assert function_callees("typed.py:construct") == {"pkg/repository.py:Repository.save"}
        assert function_callees("typed.py:Service.persist") == {
            "pkg/repository.py:Repository.save"
        }
        assert function_callees("typed.py:Container.persist") == {
            "pkg/repository.py:Repository.save"
        }
        assert function_callees("typed.py:StaticContainer.persist") == {
            "pkg/repository.py:Repository.save"
        }
        assert function_callees("typed.py:ExternalChild.run") == {
            "pkg/base.py:ExternalBase.execute"
        }
        assert function_callees("typed.py:persist_child") == {"typed.py:BaseRepo.save"}
        assert function_callees("typed.py:persist_union") == set()
        assert function_callees("typed.py:outer.inner") == set()
    finally:
        db.close()
        _db_instances.pop(key, None)


def test_typed_edges_survive_snapshot_and_repopulation(isolated_temp_dir):
    import os
    from src.database.graph_client import EngramClient
    from src.query import query

    caller = "caller.py:run"
    callee = "service.py:execute"
    with open(os.path.join(isolated_temp_dir, "caller.py"), "w") as handle:
        handle.write("def run():\n    return execute()\n")
    with open(os.path.join(isolated_temp_dir, "service.py"), "w") as handle:
        handle.write("def execute():\n    return True\n")
    client = EngramClient(isolated_temp_dir)
    client.add_node("caller.py", "File", "caller.py", "caller.py")
    client.add_node(caller, "Function", "run", "caller.py", calls=["execute"])
    client.add_node(callee, "Function", "execute", "service.py")
    client.add_structural_edge(caller, "caller.py")
    client.resolve_and_connect_calls(caller, ["execute"])
    client.build()

    assert client.get_callees(caller) == [callee]
    assert set(client.get_dependencies(caller)) == {"caller.py", callee}
    assert client.get_node_meta(caller)["calls_count"] == 1
    metadata = query(client, f'METADATA FOR "{caller}"')
    assert metadata["results"]["direct_callees"] == [callee]
    assert metadata["results"]["structural_dependencies"] == ["caller.py"]
    client.close()

    restored = EngramClient(isolated_temp_dir)
    try:
        assert restored.get_callees(caller) == [callee]
        assert set(restored.get_dependencies(caller)) == {"caller.py", callee}
        restored.repopulate_edges()
        restored.build()
        assert restored.get_callees(caller) == [callee]
        assert set(restored.get_dependencies(caller)) == {"caller.py", callee}
        assert restored.get_node_meta(caller)["calls_count"] == 1
    finally:
        restored.close()
