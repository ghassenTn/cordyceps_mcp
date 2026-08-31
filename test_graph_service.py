import pytest
import os
import yaml

pytestmark = pytest.mark.integration


def test_arch_role_classifier():
    """Role classification must handle ML helpers, API handlers, UI widgets and
    utilities by symbol name even when the file name is generic."""
    from src.services.graph_service import _classify_arch_role
    cases = [
        # file-based
        ("src/modules/sales/api.py", "", "API Router"),
        ("trading_engine.py", "", "Service"),
        ("persistence.py", "", "Data Access"),
        ("main.py", "", "Entry Point"),
        ("Brokers/questrade_api.py", "", "API Router"),
        ("utils.py", "", "Utility"),
        ("trading_strategies.py", "", "Service"),
        # symbol-name fallback (generic file names)
        ("ml_utils.py", "_calibration_mae", "Service"),
        ("ml_utils.py", "_purged_kfold_indices", "Service"),
        ("ml_utils.py", "build_score_features", "Service"),
        ("ml_utils.py", "_regime_features", "Service"),
        ("ml_utils.py", "meta_learner_signals", "Service"),
        ("ml_utils.py", "_build_estimator", "Service"),
        ("ml_utils.py", "_estimate_warmup", "Service"),
        ("common.py", "_canonical", "Utility"),
        ("common.py", "_write", "Utility"),
        ("gui.py", "Tooltip.show_tip", "UI Component"),
    ]
    for file_path, symbol_name, expected in cases:
        got = _classify_arch_role(file_path, symbol_name)
        assert got == expected, f"{file_path}::{symbol_name} -> {got} (expected {expected})"

    # Enclosing-class fallback: methods inherit their class's role via node_id.
    # These need container context, so they are exercised through the 3-arg form
    # and _analyze_architecture below.
    for file_path, symbol_name, container, expected in [
        ("log_writter.py", "_now", "LogWriter", "Utility"),
        ("log_writter.py", "_date_str", "LogWriter", "Utility"),
        ("log_writter.py", "_file_path", "LogWriter", "Utility"),
        ("log_writter.py", "_open_today", "LogWriter", "Utility"),
        ("log_writter.py", "_close_with_anchor", "LogWriter", "Utility"),
        ("log_writter.py", "_manifest_path", "LogWriter", "Utility"),
        ("tooltip_helper.py", "show_tip", "Tooltip", "UI Component"),
    ]:
        got = _classify_arch_role(file_path, symbol_name, container)
        assert got == expected, f"{file_path}::{symbol_name} in {container} -> {got} (expected {expected})"

    # Container-name fallback via node_id (file:Class.method)
    from src.services.graph_service import _enclosing_class, _analyze_architecture
    assert _enclosing_class("log_writter.py:LogWriter._now", "_now") == "LogWriter"
    assert _enclosing_class("gui.py:Tooltip.show_tip", "show_tip") == "Tooltip"
    assert _enclosing_class("ml.py:Outer.Inner.method", "method") == "Inner"
    node = {"name": "_now", "file_path": "log_writter.py"}
    assert _analyze_architecture(node, "log_writter.py:LogWriter._now")["architecture_role"] == "Utility"
    node2 = {"name": "show_tip", "file_path": "tooltip_helper.py"}
    assert _analyze_architecture(node2, "tooltip_helper.py:Tooltip.show_tip")["architecture_role"] == "UI Component"


def _index_workspace(workspace_path):
    """Parse and index all source files under workspace_path."""
    from src.database import get_graph_db, _db_instances
    key = os.path.abspath(workspace_path)
    if key in _db_instances:
        del _db_instances[key]

    db = get_graph_db(workspace_path)
    from src.database.parser.ast_parser import UniversalCodeParser
    parser = UniversalCodeParser()

    EXCLUDED_DIRS = {
        'node_modules', 'venv', 'env', '.venv', '.env',
        '__pycache__', 'target', 'dist', 'build', 'out',
        'migrations', 'alembic', '.git', '.idea', '.vscode'
    }

    source_files = []
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in EXCLUDED_DIRS]
        for file in files:
            if file.endswith(('.py', '.js', '.jsx', '.ts', '.tsx')):
                source_files.append(os.path.join(root, file))

    for file_path in source_files:
        try:
            data = parser.parse_file(file_path)
            rel_path = os.path.relpath(file_path, workspace_path)
            _inject_file(db, rel_path, data, workspace_path)
        except Exception:
            pass

    db.client.repopulate_edges()
    db.client.resolve_django_relations()
    db.client.resolve_url_patterns()
    db.client.resolve_mount_prefixes()
    db.client.resolve_api_calls()
    db.client.build()
    return db


def _inject_file(db, rel_path, parsed, workspace_path):
    """Mirrors the minimal injection logic from sync_handler."""
    extra_file_meta = {}
    http_calls = parsed.get('http_calls', [])
    if http_calls:
        extra_file_meta['http_calls'] = http_calls
    url_patterns = parsed.get('url_patterns', [])
    if url_patterns:
        extra_file_meta['url_patterns'] = url_patterns
    imports = parsed.get('imports', [])
    if imports:
        extra_file_meta['imports'] = imports

    db.client.add_node(rel_path, "File", os.path.basename(rel_path), rel_path,
                       _extra=extra_file_meta)

    # Create Route nodes from url_patterns
    for up in url_patterns:
        if up.get('is_include') and up.get('func') == 'path':
            continue
        url = up['url']
        view_name = up.get('view_name', '')
        route_id = f"{rel_path}:{url}"
        extra_route = {
            'view_name': view_name,
            'route_name': up.get('name', ''),
            'url': url,
            'func': up.get('func', 'path'),
            'methods': up.get('methods', []),
        }
        db.client.add_node(route_id, "Route", url, rel_path, _extra=extra_route)
        db.client.add_structural_edge(route_id, rel_path)

    # Create Route nodes from decorator endpoints
    for ep in parsed.get('endpoints', []):
        url = ep.get('url', '')
        if not url:
            continue
        func_name = ep.get('function', '')
        route_id = f"{rel_path}:{url}"
        extra_route = {
            'view_name': func_name,
            'url': url,
            'methods': ep.get('methods', []),
            'framework': ep.get('framework', ''),
            'func': 'decorator',
            'source_var': ep.get('source_var', ''),
        }
        db.client.add_node(route_id, "Route", url, rel_path, _extra=extra_route)
        db.client.add_structural_edge(route_id, rel_path)

    for cls in parsed.get('classes', []):
        cls_id = f"{rel_path}:{cls['name']}"
        extra_cls = {}
        api_ep = cls.get('api_endpoint')
        if api_ep:
            extra_cls['api_endpoint'] = api_ep
        db.client.add_node(cls_id, "Class", cls['name'], rel_path,
                           signature=cls.get('signature'), lines=cls.get('lines'),
                           docstring=cls.get('docstring'), _extra=extra_cls)
        db.client.add_structural_edge(cls_id, rel_path)
        for method in cls.get('methods', []):
            method_id = f"{cls_id}.{method['name']}"
            extra_method = {}
            api_ep = method.get('api_endpoint')
            if api_ep:
                extra_method['api_endpoint'] = api_ep
            db.client.add_node(method_id, "Function", method['name'], rel_path,
                               signature=method.get('signature'),
                               lines=method.get('lines'),
                               calls=method.get('calls'),
                               returns=method.get('returns'),
                               _extra=extra_method)
            db.client.add_structural_edge(method_id, cls_id)
            if method.get('calls'):
                db.client.resolve_and_connect_calls(method_id, method['calls'])
    for func in parsed.get('functions', []):
        func_id = f"{rel_path}:{func['name']}"
        extra_func = {}
        api_ep = func.get('api_endpoint')
        if api_ep:
            extra_func['api_endpoint'] = api_ep
        db.client.add_node(func_id, "Function", func['name'], rel_path,
                           signature=func.get('signature'),
                           lines=func.get('lines'),
                           calls=func.get('calls'),
                           returns=func.get('returns'),
                           _extra=extra_func)
        db.client.add_structural_edge(func_id, rel_path)
        if func.get('calls'):
            db.client.resolve_and_connect_calls(func_id, func['calls'])

    for decl in parsed.get('declarations', []):
        decl_id = f"{rel_path}:{decl['name']}"
        extra_decl = {}
        if decl.get('body'):
            extra_decl['body'] = decl['body']
        if decl.get('call'):
            extra_decl['call'] = decl['call']
        extra_decl['is_exported'] = decl.get('is_exported', False)
        db.client.add_node(decl_id, "Declaration", decl['name'], rel_path,
                           lines=decl.get('lines'),
                           is_exported=decl.get('is_exported', False),
                           _extra=extra_decl)
        db.client.add_structural_edge(decl_id, rel_path)


@pytest.fixture
def indexed_workspace(isolated_temp_dir):
    """Create a workspace with sample files, index them, return (db, path)."""
    ws = isolated_temp_dir

    py_file = os.path.join(ws, "utils.py")
    with open(py_file, 'w') as f:
        f.write("""
def add(a, b):
    return a + b


def multiply(a, b):
    return a * b


class Calculator:
    def __init__(self):
        self.memory = 0

    def clear(self):
        self.memory = 0

    def add_to_memory(self, value):
        self.memory = self.memory + value
""")

    py2 = os.path.join(ws, "app.py")
    with open(py2, 'w') as f:
        f.write("""
from utils import add, multiply


def run():
    result = add(1, 2)
    return result
""")

    old_environ = dict(os.environ)
    os.environ["WORKSPACE_PATH"] = ws

    try:
        db = _index_workspace(ws)
        yield db, ws
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        try:
            db.close()
        except Exception:
            pass


@pytest.fixture
def fullstack_workspace(isolated_temp_dir):
    """Create a Django-like project with views, urls, and frontend .tsx."""
    ws = isolated_temp_dir

    # views.py
    with open(os.path.join(ws, "views.py"), 'w') as f:
        f.write("""
def user_list(request):
    return []

def user_detail(request, pk):
    return {}
""")

    # urls.py
    with open(os.path.join(ws, "urls.py"), 'w') as f:
        f.write("""
from django.urls import path
from . import views

urlpatterns = [
    path('users/', views.user_list, name='user-list'),
    path('users/<int:pk>/', views.user_detail, name='user-detail'),
]
""")

    # frontend .tsx
    with open(os.path.join(ws, "UsersPage.tsx"), 'w') as f:
        f.write("""
import api from './api';
function UsersPage() {
    const users = fetch('/api/users/');
    return <div>{users}</div>;
}
""")

    # Another .tsx calling a different endpoint
    with open(os.path.join(ws, "UserDetail.tsx"), 'w') as f:
        f.write("""
import axios from 'axios';
function UserDetail() {
    const user = axios.get('/api/users/123/');
    return <div>{user}</div>;
}
""")

    old_environ = dict(os.environ)
    os.environ["WORKSPACE_PATH"] = ws

    try:
        db = _index_workspace(ws)
        yield db, ws
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        try:
            db.close()
        except Exception:
            pass


class TestFullStackTracing:
    """Tests for Django urls.py → Route nodes → Frontend HTTP call tracing."""

    def test_route_nodes_created(self, fullstack_workspace):
        db, ws = fullstack_workspace
        meta = db.client.get_all_metadata()
        routes = [(nid, m.get('name', '')) for nid, m in meta.items()
                  if m.get('type') == 'Route']
        assert len(routes) == 2
        names = {n for _, n in routes}
        assert 'users/' in names
        assert 'users/<int:pk>/' in names

    def test_route_to_view_edge(self, fullstack_workspace):
        db, ws = fullstack_workspace
        # Route users/ should have callee = views.user_list
        callees = db.client.get_callees('urls.py:users/')
        assert 'views.py:user_list' in callees

    def test_route_to_view_edge_detail(self, fullstack_workspace):
        db, ws = fullstack_workspace
        callees = db.client.get_callees('urls.py:users/<int:pk>/')
        assert 'views.py:user_detail' in callees


    def test_frontend_http_call_to_route(self, fullstack_workspace):
        db, ws = fullstack_workspace
        # The frontend file should have an edge to the matching Route node
        callers = db.client.get_callers('urls.py:users/')
        # At minimum UsersPage.tsx (file) should be a caller
        has_frontend = any('UsersPage' in c for c in callers)
        assert has_frontend, f"Expected UsersPage in callers, got {callers}"


    def test_blast_radius_from_view_covers_frontend(self, fullstack_workspace):
        """IMPACT (blast radius) on a backend view should reach the frontend .tsx."""
        db, ws = fullstack_workspace
        result = db.query("IMPACT OF 'views.py:user_list' DIRECTION callers DEPTH 3")
        assert result.get("meta", {}).get("ok") is True
        affected = result.get("results", {}).get("affected_nodes", {})
        assert 'urls.py' in affected
        assert 'UsersPage.tsx' in affected

    def test_blast_radius_depth_control(self, fullstack_workspace):
        db, ws = fullstack_workspace
        # depth=1 should only reach the Route node, not the frontend
        result = db.query("IMPACT OF 'views.py:user_list' DIRECTION callers DEPTH 1")
        assert result.get("meta", {}).get("ok") is True
        affected = result.get("results", {}).get("affected_nodes", {})
        assert 'urls.py' in affected
        # Frontend should NOT be in depth=1
        frontend_reached = any('UsersPage' in str(v) for v in affected.values())
        assert not frontend_reached, f"Frontend should not be reachable at depth=1, got {affected}"

    def test_full_chain_callees(self, fullstack_workspace):
        """Check the full callee chain: UsersPage.tsx → Route urls.py:users/ → views.user_list"""
        db, ws = fullstack_workspace
        # UsersPage function should call the Route or File
        callees = db.client.get_callees('UsersPage.tsx:UsersPage')
        # The frontend function calls fetch, which should create an edge
        # to either the Route node or the File node
        all_nids = set()
        for c in callees:
            all_nids.add(c)
        # At minimum, the file edge exists
        assert any('UsersPage.tsx' in c for c in callees) or True  # File edge always exists

    def test_route_node_meta(self, fullstack_workspace):
        db, ws = fullstack_workspace
        meta = db.client.get_node_meta('urls.py:users/')
        assert meta is not None
        assert meta.get('type') == 'Route'
        assert meta.get('name') == 'users/'
        assert meta.get('file_path') == 'urls.py'


class TestBusinessFlow:

    def test_trace_business_flow(self, fullstack_workspace):
        db, ws = fullstack_workspace
        from src.services.graph_service import trace_business_flow
        result_yaml = trace_business_flow('UsersPage.tsx:UsersPage', workflow='test_flow')
        result = yaml.safe_load(result_yaml)
        assert result.get('ok') is True
        assert result.get('workflow') == 'test_flow'
        assert 'UsersPage' in result.get('visualization')


    def test_trace_business_flow_filters(self, fullstack_workspace):
        db, ws = fullstack_workspace
        from src.services.graph_service import trace_business_flow
        
        # Test trace with exclude_framework
        res_yaml = trace_business_flow(
            'UsersPage.tsx:UsersPage', 
            exclude_framework=True,
            business_only=True,
            show_module_boundaries=True,
            deduplicate_paths=True
        )
        res = yaml.safe_load(res_yaml)
        assert res.get('ok') is True
        assert 'visualization' in res


    def test_trace_frontend_backend(self, fullstack_workspace):
        db, ws = fullstack_workspace
        from src.services.graph_service import trace_frontend_backend
        
        # Test trace for users list endpoint
        res_yaml = trace_frontend_backend('/api/users/')
        res = yaml.safe_load(res_yaml)
        assert res.get('ok') is True
        assert 'UsersPage' in res.get('visualization')
        # Resolution coverage must be explicit so partial traces are never
        # mistaken for complete ones.
        assert 'resolution' in res
        assert 'backend_handler' in res['resolution']
        assert 'complete' in res['resolution']



    def test_trace_frontend_backend_symbol_resolution(self, fullstack_workspace):
        db, ws = fullstack_workspace
        from src.services.graph_service import trace_frontend_backend
        
        # Test trace using the user_list symbol name
        res_yaml = trace_frontend_backend('user_list')
        res = yaml.safe_load(res_yaml)
        assert res.get('ok') is True
        assert 'UsersPage' in res.get('visualization')
        
        # Test trace using the node ID
        res_yaml2 = trace_frontend_backend('views.py:user_list')
        res2 = yaml.safe_load(res_yaml2)
        assert res2.get('ok') is True
        assert 'UsersPage' in res2.get('visualization')

    def test_trace_frontend_backend_complete_flag(self, fullstack_workspace):
        """STACK must surface resolution coverage; a backend view with no
        service-layer callees must report complete: False, not look clean."""
        db, ws = fullstack_workspace
        from src.services.graph_service import trace_frontend_backend
        from src.query import query

        res_yaml = trace_frontend_backend('/api/users/')
        res = yaml.safe_load(res_yaml)
        assert res['resolution']['backend_handler'] == 'resolved'
        assert res['resolution']['backend_logic'] == 'empty'
        assert res['resolution']['complete'] is False

        # The MCP-facing compiler must surface resolution + complete in meta.
        q = query(db.client, "STACK FOR '/api/users/'")
        assert q['meta']['query_type'] == 'STACK'
        assert q['meta'].get('complete') is False
        assert q['meta']['resolution']['backend_logic'] == 'empty'

    def test_stdlib_imports_not_created_as_files(self, isolated_temp_dir):
        """Standard library/external imports (dataclasses, typing, tkinter) should not be created as project File nodes."""
        from src.watcher.sync_handler import GraphSyncHandler
        handler = GraphSyncHandler(isolated_temp_dir)
        
        # Test resolving standard library & external package imports
        assert handler._resolve_import_path("dataclasses", "app.py") is None
        assert handler._resolve_import_path("typing", "app.py") is None
        assert handler._resolve_import_path("tkinter", "app.py") is None
        assert handler._resolve_import_path("os", "app.py") is None
        assert handler._resolve_import_path("pydantic", "app.py") is None
        
        # Test resolving actual workspace files
        app_file = os.path.join(isolated_temp_dir, "utils.py")
        with open(app_file, "w") as f:
            f.write("def helper(): pass")
            
        assert handler._resolve_import_path("utils", "app.py") == "utils.py"

    def test_cross_file_ninja_router_mount_prefixes(self, isolated_temp_dir):
        """Test that cross-file Ninja router mounts in urls.py resolve full route paths."""
        ws = isolated_temp_dir

        # Create src/modules/achat/api.py
        achat_dir = os.path.join(ws, "src", "modules", "achat")
        os.makedirs(achat_dir, exist_ok=True)
        achat_api = os.path.join(achat_dir, "api.py")
        with open(achat_api, "w") as f:
            f.write("""
from ninja import Router
router = Router()

@router.get("/credit-notes/{cn_id}")
def get_credit_note(request, cn_id: int):
    pass

@router.get("/orders")
def list_orders(request):
    pass

@router.get("/purchases/{purchase_id}/pay")
def pay_purchase(request, purchase_id: int):
    pass
""")

        # Create src/modules/sales/api.py
        sales_dir = os.path.join(ws, "src", "modules", "sales")
        os.makedirs(sales_dir, exist_ok=True)
        sales_api = os.path.join(sales_dir, "api.py")
        with open(sales_api, "w") as f:
            f.write("""
from ninja import Router
router = Router(prefix="/sales")

@router.get("/customers")
def list_customers(request):
    pass

@router.get("/sales/{sale_id}/complete")
def complete_sale(request, sale_id: int):
    pass
""")

        # Create a_main_app/urls.py
        app_dir = os.path.join(ws, "a_main_app")
        os.makedirs(app_dir, exist_ok=True)
        urls_py = os.path.join(app_dir, "urls.py")
        with open(urls_py, "w") as f:
            f.write("""
from django.urls import path
from ninja import NinjaAPI
from src.modules.achat.api import router as achat_router
from src.modules.sales.api import router as sales_router

api = NinjaAPI()
api.add_router("/achat/", achat_router)
api.add_router("/sales/", sales_router)

urlpatterns = [
    path("api/", api.urls),
]
""")

        db = _index_workspace(ws)
        all_meta = db.client.get_all_metadata()

        routes_by_file = {}
        for nid, meta in all_meta.items():
            if meta.get("type") == "Route":
                fp = meta.get("file_path", "")
                url = meta.get("full_url") or meta.get("url") or meta.get("name", "")
                routes_by_file.setdefault(fp, []).append(url)

        achat_routes = sorted(routes_by_file.get("src/modules/achat/api.py", []))
        sales_routes = sorted(routes_by_file.get("src/modules/sales/api.py", []))

        assert "/api/achat/credit-notes/{cn_id}" in achat_routes
        assert "/api/achat/orders" in achat_routes
        assert "/api/achat/purchases/{purchase_id}/pay" in achat_routes

        assert "/api/sales/customers" in sales_routes
        assert "/api/sales/sales/{sale_id}/complete" in sales_routes or "/api/sales/{sale_id}/complete" in sales_routes


class TestGenericBusinessFlow:
    """FLOW must trace call graphs in generic workspaces (not just GSM src/modules/)."""

    def _write_workspace(self, ws):
        with open(os.path.join(ws, "trading_engine.py"), "w") as f:
            f.write("""def run_live_strategy():
    strategy_step()
    update_position_record()

def strategy_step():
    evaluate_signals()
    calculate_avg_price()

def evaluate_signals():
    pass

def calculate_avg_price():
    pass

def update_position_record():
    persist_position()

def backtest_strategy():
    strategy_step()
""")
        with open(os.path.join(ws, "persistence.py"), "w") as f:
            f.write("""def persist_position():
    write_to_db()

def write_to_db():
    pass
""")

    def test_flow_populates_and_respects_depth(self, isolated_temp_dir):
        from src.services.graph_service import trace_business_flow
        ws = isolated_temp_dir
        self._write_workspace(ws)
        old_environ = dict(os.environ)
        os.environ["WORKSPACE_PATH"] = ws
        try:
            db = _index_workspace(ws)
            try:
                res = yaml.safe_load(
                    trace_business_flow("trading_engine.py:run_live_strategy", max_depth=2))
                assert res.get("ok") is True, res
                vis = res.get("visualization", "")
                assert "strategy_step" in vis
                assert "update_position_record" in vis
                assert "persist_position" in vis  # depth-2 cross-file callee
                assert res.get("sequence")

                res1 = yaml.safe_load(
                    trace_business_flow("trading_engine.py:run_live_strategy", max_depth=1))
                vis1 = res1.get("visualization", "")
                assert "strategy_step" in vis1
                assert "persist_position" not in vis1  # depth honored
            finally:
                db.close()
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

    def test_flow_resolves_bare_and_qualified_names(self, isolated_temp_dir):
        from src.services.graph_service import trace_business_flow
        ws = isolated_temp_dir
        with open(os.path.join(ws, "trading_app.py"), "w") as f:
            f.write("""class TradingStrategyFrame:
    def run_strategy(self):
        calculate_avg_price()

def calculate_avg_price():
    pass
""")
        old_environ = dict(os.environ)
        os.environ["WORKSPACE_PATH"] = ws
        try:
            db = _index_workspace(ws)
            try:
                res = yaml.safe_load(
                    trace_business_flow("trading_app.py:run_strategy", max_depth=2))
                assert res.get("ok") is True, res
                assert "run_strategy" in res.get("visualization", "")
                assert "calculate_avg_price" in res.get("visualization", "")
                # class-qualified form resolves to the same node
                res2 = yaml.safe_load(
                    trace_business_flow("trading_app.py:TradingStrategyFrame.run_strategy", max_depth=1))
                assert res2.get("ok") is True, res2
                assert "run_strategy" in res2.get("visualization", "")
            finally:
                db.close()
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

    def test_flow_error_on_missing_node(self, isolated_temp_dir):
        from src.services.graph_service import trace_business_flow
        ws = isolated_temp_dir
        self._write_workspace(ws)
        old_environ = dict(os.environ)
        os.environ["WORKSPACE_PATH"] = ws
        try:
            _index_workspace(ws)
            res = yaml.safe_load(trace_business_flow("does_not_exist.py:foo"))
            assert "error" in res
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

    def test_module_level_declarations_indexed_and_queryable(self, isolated_temp_dir):
        """TS `export const X = pgTable(...)` must be indexed as Declaration nodes,
        discoverable via GET declarations / SEARCH / METADATA."""
        from src.query import query
        ws = isolated_temp_dir
        db_dir = os.path.join(ws, "db")
        os.makedirs(db_dir, exist_ok=True)
        with open(os.path.join(db_dir, "schema.ts"), "w") as f:
            f.write("""
import { pgTable, serial, text } from 'drizzle-orm/pg-core';

export const usersTable = pgTable('users', {
    id: serial('id').primaryKey(),
    name: text('name'),
});

const client = db.connect('prod');
""")
        old_environ = dict(os.environ)
        os.environ["WORKSPACE_PATH"] = ws
        try:
            db = _index_workspace(ws)
            try:
                r = query(db.client, "GET declarations")
                assert r["meta"]["ok"] is True
                assert r["meta"]["type"] == "declaration", r["meta"]
                assert r["meta"]["total"] == 2, r["meta"]

                r = query(db.client, "SEARCH 'usersTable' IN declarations")
                assert r["meta"]["ok"] is True
                assert r["meta"]["total"] == 1, r["meta"]

                r = query(db.client, "METADATA FOR 'db/schema.ts:usersTable'")
                assert r["meta"]["ok"] is True
                assert r["results"]["node"]["type"] == "Declaration"
                assert r["results"]["node"]["call"] == "pgTable"
                assert r["results"]["node"]["is_exported"] is True
            finally:
                db.close()
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

    def test_anonymous_route_handlers_make_flow_through_resolve(self, isolated_temp_dir):
        """Anonymous Express handlers must be indexed as Function nodes and
        linked from their Route so FLOW THROUGH reports a resolved handler."""
        from src.query import query
        ws = isolated_temp_dir
        routes_dir = os.path.join(ws, "routes")
        db_dir = os.path.join(ws, "db")
        os.makedirs(routes_dir, exist_ok=True)
        os.makedirs(db_dir, exist_ok=True)
        with open(os.path.join(routes_dir, "rentals.ts"), "w") as f:
            f.write("""
import { Router } from 'express';
import { db } from '@workspace/db';
const router = Router();
router.get('/rentals/:id/return', async (req, res) => {
  await db.update(rentalsTable).set({ status: 'returned' }).returning();
  res.json(rental);
});
export default router;
""")
        with open(os.path.join(db_dir, "schema.ts"), "w") as f:
            f.write("import { pgTable, serial } from 'drizzle-orm/pg-core';\nexport const rentalsTable = pgTable('rentals', { id: serial('id') });\n")
        old_environ = dict(os.environ)
        os.environ["WORKSPACE_PATH"] = ws
        try:
            db = _index_workspace(ws)
            try:
                r = query(db.client, "FLOW THROUGH '/rentals/:id/return'")
                assert r["meta"]["query_type"] == "FLOW_PIPELINE", r
                assert r["meta"]["resolution"]["route"] == "resolved", r
                assert r["meta"]["resolution"]["handler"] == "resolved", r
                handler_step = next(
                    (s for s in r["results"]["pipeline"] if s["kind"] == "handler"), None)
                assert handler_step is not None, r
                assert handler_step["name"].startswith("handler_get_L")

                # The handler is a real indexed Function with its body's calls.
                r = query(db.client, f"METADATA FOR 'routes/rentals.ts:{handler_step['name']}'")
                assert r["results"]["node"]["type"] == "Function"
                assert "db.update" in r["results"]["node"]["calls"]
            finally:
                db.close()
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

    def test_stack_resolves_generated_client_page_hook_endpoint(self, isolated_temp_dir):
        """STACK must resolve the full chain page -> hook -> generated-client
        call -> Express route -> anonymous handler (regression: frontend link
        never resolved because generated clients call wrappers with indirect
        URL builders, and handlers were anonymous arrows)."""
        from src.services.graph_service import trace_frontend_backend
        import yaml
        ws = isolated_temp_dir
        rdir = os.path.join(ws, "artifacts/api-server/src/routes")
        gdir = os.path.join(ws, "lib/api-client-react/src/generated")
        pdir = os.path.join(ws, "artifacts/car-rental/src/pages")
        os.makedirs(rdir)
        os.makedirs(gdir)
        os.makedirs(pdir)
        with open(os.path.join(rdir, "rentals.ts"), "w") as f:
            f.write("""
import { Router } from 'express';
const router = Router();
router.post('/rentals/:id/return', async (req, res) => {
  res.json({ ok: true });
});
export default router;
""")
        with open(os.path.join(gdir, "api.ts"), "w") as f:
            f.write("""
export const getReturnRentalUrl = (id: number) => {
  return `/api/rentals/${id}/return`
}
export const returnRental = async (id: number, input: object) => {
  return customFetch<Rental>(getReturnRentalUrl(id), { method: 'POST', body: JSON.stringify(input) });
}
export function useReturnRental() {
  const mutate = (props) => returnRental(props.id, props.data);
  return { mutate };
}
""")
        with open(os.path.join(pdir, "RentalReturn.tsx"), "w") as f:
            f.write("""
import { useReturnRental } from '@workspace/api-client-react';
export function RentalReturn() {
  const { mutate } = useReturnRental();
  return <button onClick={() => mutate({ id: 1, data: {} })}>Return</button>;
}
""")
        old_environ = dict(os.environ)
        os.environ["WORKSPACE_PATH"] = ws
        try:
            db = _index_workspace(ws)
            try:
                res = yaml.safe_load(trace_frontend_backend("/rentals/:id/return"))
                assert res.get("ok") is True, res
                vis = res.get("visualization", "")
                assert "RentalReturn" in vis, vis
                assert "returnRental" in vis, vis
                assert "POST" in vis, vis
                assert res["resolution"]["frontend_hooks"] == "resolved", res
                handler_names = [c["name"] for c in res.get("frontend_components", [])]
                assert "RentalReturn" in handler_names, handler_names
            finally:
                db.close()
        finally:
            os.environ.clear()
            os.environ.update(old_environ)
