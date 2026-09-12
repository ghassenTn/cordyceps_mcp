"""Integration tests: real parser + Rust engine + WorkspaceIndex + structural tools.

Each test writes a small workspace to a temp dir, indexes it through the same
``WorkspaceIndex`` the server uses, and asserts on graph facts and on what the
three tools report. These are the regression guards for call resolution,
framework linking, persistence and incremental sync.
"""
import json
import os

import pytest

from src.database import _db_instances
from src.database.graph_client import EngramClient, INDEX_META_FILENAME
from src.indexing import WorkspaceIndex
from src.structure import CodeStructure
from src.watcher.sync_handler import get_sync_queue

pytestmark = pytest.mark.integration


# ── helpers ──────────────────────────────────────────────────────────────

def _write(ws: str, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(source)


def _release(ws: str) -> None:
    db = _db_instances.pop(os.path.abspath(ws), None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


@pytest.fixture
def indexer(isolated_temp_dir, monkeypatch):
    """Factory: ``build(files) -> (WorkspaceIndex, CodeStructure)``; cleans up singletons."""
    ws = isolated_temp_dir
    monkeypatch.setenv("WORKSPACE_PATH", ws)
    _release(ws)
    # Drain anything a previous test left in the process-global queue.
    q = get_sync_queue()
    while not q.empty():
        q.get_nowait()

    def build(files: dict[str, str] | None = None):
        if files:
            _write(ws, files)
        _release(ws)
        index = WorkspaceIndex(ws)
        index.initial_scan()
        return index, CodeStructure(index.client, ws, health=index.health_warnings)

    build.ws = ws
    yield build
    _release(ws)


FULLSTACK = {
    "views.py": """
def user_list(request):
    return []

def user_detail(request, pk):
    return {}
""",
    "urls.py": """
from django.urls import path
from . import views

urlpatterns = [
    path('users/', views.user_list, name='user-list'),
    path('users/<int:pk>/', views.user_detail, name='user-detail'),
]
""",
    "UsersPage.tsx": """
import api from './api';
function UsersPage() {
    const users = fetch('/api/users/');
    return <div>{users}</div>;
}
""",
    "UserDetail.tsx": """
import axios from 'axios';
function UserDetail() {
    const user = axios.get('/api/users/123/');
    return <div>{user}</div>;
}
""",
}


# ── call resolution (Python contextual resolver) ─────────────────────────

def test_python_contextual_call_resolution_end_to_end(indexer):
    index, _ = indexer({
        "pkg/__init__.py": "",
        "pkg/a.py": "def work():\n    return 'a'\n",
        "pkg/b.py": "def work():\n    return 'b'\n",
        "pkg/repository.py": "class Repository:\n    def save(self):\n        return True\n",
        "pkg/base.py": "class ExternalBase:\n    def execute(self):\n        return True\n",
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
    })
    client = index.client

    def function_callees(node_id):
        return {c for c in client.get_callees(node_id)
                if (client.get_node_meta(c) or {}).get("type") == "Function"}

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
    assert "nested.py:parent" in client.get_dependencies("nested.py:parent.child")
    assert function_callees("typed.py:persist") == {"pkg/repository.py:Repository.save"}
    assert function_callees("typed.py:construct") == {"pkg/repository.py:Repository.save"}
    assert function_callees("typed.py:Service.persist") == {"pkg/repository.py:Repository.save"}
    assert function_callees("typed.py:Container.persist") == {"pkg/repository.py:Repository.save"}
    assert function_callees("typed.py:StaticContainer.persist") == {"pkg/repository.py:Repository.save"}
    assert function_callees("typed.py:ExternalChild.run") == {"pkg/base.py:ExternalBase.execute"}
    assert function_callees("typed.py:persist_child") == {"typed.py:BaseRepo.save"}
    assert function_callees("typed.py:persist_union") == set()
    assert function_callees("typed.py:outer.inner") == set()


def test_lookup_and_impact_on_resolved_python_graph(indexer):
    _, structure = indexer({
        "pkg/__init__.py": "",
        "pkg/a.py": "def work():\n    return 'a'\n",
        "pkg/b.py": "def work():\n    return 'b'\n",
        "caller.py": "from pkg.a import work\n\ndef call_a():\n    return work()\n",
        "top.py": "from caller import call_a\n\ndef main():\n    call_a()\n",
    })
    ambiguous = structure.lookup("work")
    assert ambiguous["status"] == "ambiguous"
    assert [c["id"] for c in ambiguous["candidates"]] == ["pkg/a.py:work", "pkg/b.py:work"]

    exact = structure.lookup("pkg/a.py:work")
    assert exact["status"] == "resolved"
    assert exact["relationships"]["callers"] == ["caller.py:call_a"]
    assert exact["symbol"]["lines"] == "1-2"

    blast = structure.impact("pkg/a.py:work", depth=5)
    assert blast["meta"]["direct"] == 1 and blast["meta"]["total"] == 2
    assert blast["affected"] == {
        "caller.py": ["call_a [Function] L3-4 depth=1"],
        "top.py": ["main [Function] L3-4 depth=2"],
    }
    # b.py:work is untouched by the shared bare name.
    assert structure.impact("pkg/b.py:work", depth=5)["meta"]["total"] == 0

    # Imports are structural: caller.py imports pkg/a.py but that is not "impact".
    rel = structure.lookup("caller.py")["relationships"]
    assert rel["imports"] == ["pkg/a.py"]
    assert structure.lookup("pkg/a.py")["relationships"]["imported_by"] == ["caller.py"]


def test_javascript_typescript_contextual_call_resolution_end_to_end(indexer):
    index, structure = indexer({
        "a.ts": """
export function work() { return 'a'; }
export class Repo { save() { return true; } }
export default class Service { run() { return work(); } }
""",
        "b.ts": "export function work() { return 'b'; }\n",
        "remote.ts": "export function remoteOnly() { return false; }\n",
        "caller.ts": """
import Service, { work as runA, Repo } from './a.js';
import * as B from './b';

export function callA() { return runA(); }
export function callB() { return B.work(); }
export function unknown(obj: any) { return obj.work(); }
export function unimported() { return remoteOnly(); }
export function shadowed(runA: () => string) { return runA(); }
export function construct() { const service = new Service(); return service.run(); }
export function typed(repo: Repo) { return repo.save(); }

class Base { base() { return true; } }
class Child extends Base {
    own() { return this.help(); }
    help() { return true; }
    parent() { return super.base(); }
    #secret() { return true; }
    reveal() { return this.#secret(); }
}

export function outer() {
    function inner() { return runA(); }
    return inner();
}
""",
    })
    client = index.client

    assert client.get_callees("caller.ts:callA") == ["a.ts:work"]
    assert client.get_callees("caller.ts:callB") == ["b.ts:work"]
    assert client.get_callees("caller.ts:unknown") == []
    assert client.get_callees("caller.ts:unimported") == []
    assert client.get_callees("caller.ts:shadowed") == []
    assert client.get_callees("caller.ts:construct") == ["a.ts:Service.run"]
    assert client.get_callees("caller.ts:typed") == ["a.ts:Repo.save"]
    assert client.get_callees("caller.ts:Child.own") == ["caller.ts:Child.help"]
    assert client.get_callees("caller.ts:Child.parent") == ["caller.ts:Base.base"]
    assert client.get_callees("caller.ts:Child.reveal") == ["caller.ts:Child.#secret"]
    assert client.get_callees("caller.ts:outer") == ["caller.ts:outer.inner"]
    assert client.get_callees("caller.ts:outer.inner") == ["a.ts:work"]
    assert client.get_callees("a.ts:Service.run") == ["a.ts:work"]

    native = client.engine.get_node_meta("caller.ts:callA")
    assert "calls" not in native
    assert client.get_node_meta("caller.ts:callA")["calls"] == ["runA"]
    assert "caller.ts" in structure.lookup("a.ts")["relationships"]["imported_by"]

    blast = structure.impact("a.ts:work", depth=5)
    assert "caller.ts" in blast["affected"]
    assert not any("name" in warning and "false positives" in warning
                   for warning in blast["meta"].get("warnings", []))

    _release(indexer.ws)
    restored = WorkspaceIndex(indexer.ws)
    summary = restored.initial_scan()
    assert summary["warm_start"] is True
    assert restored.client.get_callees("caller.ts:callA") == ["a.ts:work"]
    assert restored.client.get_callees("caller.ts:unknown") == []
    assert "calls" not in restored.client.engine.get_node_meta("caller.ts:callA")
    assert restored.client.get_node_meta("caller.ts:callA")["calls"] == ["runA"]


def test_javascript_commonjs_and_jsx_default_resolution(indexer):
    index, _ = indexer({
        "lib.js": """
function work() { return true; }
module.exports = { work };
""",
        "default-lib.js": """
function real() { return true; }
function fake() { return false; }
module.exports = real; // module.exports = fake
""",
        "other-lib.js": "function work() { return false; }\nmodule.exports = { work };\n",
        "common.js": """
const lib = require('./lib');
const { work: run } = require('./lib');
export function namespaceCall() { return lib.work(); }
export function namedCall() { return run(); }
const defaultFn = require('./default-lib');
export function defaultCall() { return defaultFn(); }
""",
        "mutable.js": """
let lib = require('./lib');
lib = require('./other-lib');
let unknown = require('./lib');
unknown = createClient();
export function moved() { return lib.work(); }
export function unresolved() { return unknown.work(); }
""",
        "barrel.js": "export { work as barrelWork } from './lib';\n",
        "reexport.js": """
import { barrelWork } from './barrel';
export function viaBarrel() { return barrelWork(); }
""",
        "Button.tsx": "export default function Button() { return <button />; }\n",
        "ui.tsx": "export function Panel() { return <section />; }\n",
        "anonymous.ts": "function helper() { return true; }\nexport default () => helper();\n",
        "anonymous-user.ts": """
import anonymousTask from './anonymous';
export function invoke() { return anonymousTask(); }
""",
        "App.tsx": """
import Button from './Button';
import * as UI from './ui';
export function App() { return <><Button /><UI.Panel /></>; }
""",
    })
    assert index.client.get_callees("common.js:namespaceCall") == ["lib.js:work"]
    assert index.client.get_callees("common.js:namedCall") == ["lib.js:work"]
    assert index.client.get_callees("common.js:defaultCall") == ["default-lib.js:real"]
    assert index.client.get_callees("mutable.js:moved") == ["other-lib.js:work"]
    assert index.client.get_callees("mutable.js:unresolved") == []
    assert index.client.get_callees("reexport.js:viaBarrel") == ["lib.js:work"]
    assert set(index.client.get_callees("App.tsx:App")) == {
        "Button.tsx:Button", "ui.tsx:Panel"}
    assert index.client.get_callees("anonymous-user.ts:invoke") == [
        "anonymous.ts:anonymous_L2"]
    assert index.client.get_callees("anonymous.ts:anonymous_L2") == [
        "anonymous.ts:helper"]


def test_javascript_import_change_replaces_contextual_edge(indexer):
    index, _ = indexer({
        "a.ts": "export function work() { return 'a'; }\n",
        "b.ts": "export function work() { return 'b'; }\n",
        "caller.ts": "import { work } from './a';\nexport function run() { return work(); }\n",
    })
    assert index.client.get_callees("caller.ts:run") == ["a.ts:work"]

    caller = os.path.join(indexer.ws, "caller.ts")
    _write(indexer.ws, {
        "caller.ts": "import { work } from './b';\nexport function run() { return work(); }\n"})
    get_sync_queue().put(("update", caller))
    index.drain_pending_events()
    assert index.client.get_callees("caller.ts:run") == ["b.ts:work"]
    assert "caller.ts" in CodeStructure(index.client, indexer.ws).lookup(
        "b.ts")["relationships"]["imported_by"]


def test_typescript_advanced_context_stays_conservative(indexer):
    index, structure = indexer({
        "dep.ts": "export function work() { return true; }\n",
        "repo.ts": "export class Repo { save() { return 'repo'; } }\n",
        "base.ts": "export class Base { inherited() { return true; } }\n",
        "child.ts": """
import { Base } from './base';
export class Child extends Base {}
export class GenericChild extends Base<string> {}
""",
        "qualified.ts": """
import * as models from './base';
export class QualifiedChild extends models.Base {}
""",
        "barrel.ts": "export * from './dep';\n",
        "setup.ts": "globalThis.ready = true;\n",
        "advanced.ts": """
import * as ns from './dep';
import type { Repo } from './repo';
import { Child } from './child';
import { GenericChild } from './child';
import { QualifiedChild } from './qualified';
import { work as barrelWork } from './barrel';
import { work as importedWork } from './dep';
import './setup';

class Other { save() { return 'other'; } }
class ConstructorService {
    constructor() { this.repo = new Repo(); }
    persist() { return this.repo.save(); }
    overwritten() { this.repo = new Other(); return this.repo.save(); }
}
class ConditionalService {
    constructor(flag: boolean) {
        if (flag) this.repo = new Repo();
        else this.repo = new Other();
    }
    persist() { return this.repo.save(); }
}
class UnknownConstructor {
    constructor() { this.repo = new Repo(); this.repo = createRepo(); }
    persist() { return this.repo.save(); }
}
class Holder {
    help() { return true; }
    run() { function inner() { return this.help(); } return inner(); }
}
class MethodOverride {
    save() { return true; }
    run(callback: () => boolean) { this.save = callback; return this.save(); }
}
class ArrowHolder {
    constructor() { this.repo = new Repo(); }
    run() { const callback = () => this.repo.save(); return callback(); }
}
function make() { return {}; }
function work() { return false; }
function reassignedFunction() { work = make; return work(); }
function lexicalOuter() {
    function target() { return true; }
    function inner(target: () => boolean) { return target(); }
    return inner(target);
}

export function shadowed(ns: any) { return ns.work(); }
export function typed(repo: Repo) { return repo.save(); }
export function inherited(child: Child) { return child.inherited(); }
export function genericInherited(child: GenericChild) { return child.inherited(); }
export function qualifiedInherited(child: QualifiedChild) { return child.inherited(); }
export function nonNull(repo: Repo) { return repo!.save(); }
export function parenthesized(repo: Repo) { return (repo).save(); }
export function reassigned() {
    let value = new Repo();
    value = new Other();
    return value.save();
}
export function dynamic() { return make().work(); }
export function callback(items: number[]) { return items.map(item => barrelWork()); }
export function typedCallback(items: Repo[]) { return items.map((repo: Repo) => repo.save()); }
export function callbackShadow(items: number[]) { return items.map(importedWork => importedWork()); }
export function callbackFunction(items: number[]) {
    return items.map(function(importedWork) { return importedWork(); });
}
export function outerCapture(importedWork: () => boolean) {
    function inner() { return importedWork(); }
    return inner();
}
export function destructured({ importedWork }: any) { return importedWork(); }
export function localDestructure(source: any) {
    const { importedWork } = source;
    return importedWork();
}
export function beforeAssignment(value: any) {
    value.save();
    value = new Repo();
}
""",
    })
    client = index.client
    assert client.get_callees("advanced.ts:shadowed") == []
    assert client.get_callees("advanced.ts:typed") == ["repo.ts:Repo.save"]
    assert client.get_callees("advanced.ts:inherited") == ["base.ts:Base.inherited"]
    assert client.get_callees("advanced.ts:genericInherited") == ["base.ts:Base.inherited"]
    assert client.get_callees("advanced.ts:qualifiedInherited") == ["base.ts:Base.inherited"]
    assert client.get_callees("advanced.ts:nonNull") == ["repo.ts:Repo.save"]
    assert client.get_callees("advanced.ts:parenthesized") == ["repo.ts:Repo.save"]
    assert client.get_callees("advanced.ts:reassigned") == []
    assert client.get_callees("advanced.ts:dynamic") == ["advanced.ts:make"]
    callback_node = client.get_callees("advanced.ts:callback")[0]
    assert "callback_L" in callback_node
    assert client.get_callees(callback_node) == ["dep.ts:work"]
    typed_callback_node = client.get_callees("advanced.ts:typedCallback")[0]
    assert client.get_callees(typed_callback_node) == ["repo.ts:Repo.save"]
    shadow_callback_node = client.get_callees("advanced.ts:callbackShadow")[0]
    assert client.get_callees(shadow_callback_node) == []
    function_callback_node = client.get_callees("advanced.ts:callbackFunction")[0]
    assert client.get_callees(function_callback_node) == []
    assert client.get_callees("advanced.ts:outerCapture.inner") == []
    assert client.get_callees("advanced.ts:ConstructorService.persist") == ["repo.ts:Repo.save"]
    assert client.get_callees("advanced.ts:ConstructorService.overwritten") == []
    assert client.get_callees("advanced.ts:ConditionalService.persist") == []
    assert client.get_callees("advanced.ts:UnknownConstructor.persist") == []
    assert client.get_callees("advanced.ts:Holder.run.inner") == []
    assert client.get_callees("advanced.ts:ArrowHolder.run.callback") == ["repo.ts:Repo.save"]
    assert client.get_callees("advanced.ts:MethodOverride.run") == []
    assert client.get_callees("advanced.ts:reassignedFunction") == []
    assert client.get_callees("advanced.ts:lexicalOuter.inner") == []
    assert client.get_callees("advanced.ts:destructured") == []
    assert client.get_callees("advanced.ts:localDestructure") == []
    assert client.get_callees("advanced.ts:beforeAssignment") == []
    assert "setup.ts" in structure.lookup("advanced.ts")["relationships"]["imports"]


def test_javascript_new_module_ambiguity_removes_edges_without_reparsing_caller(indexer):
    index, structure = indexer({
        "foo.ts": "export function work() { return 'ts'; }\n",
        "caller.ts": "import { work } from './foo';\nexport function run() { return work(); }\n",
    })
    assert index.client.get_callees("caller.ts:run") == ["foo.ts:work"]
    assert structure.lookup("caller.ts")["relationships"]["imports"] == ["foo.ts"]

    js_path = os.path.join(indexer.ws, "foo.js")
    _write(indexer.ws, {"foo.js": "export function work() { return 'js'; }\n"})
    get_sync_queue().put(("update", js_path))
    index.drain_pending_events()
    assert index.client.get_callees("caller.ts:run") == []
    assert structure.lookup("caller.ts")["relationships"]["imports"] == []

    caller_path = os.path.join(indexer.ws, "caller.ts")
    _write(indexer.ws, {
        "caller.ts": "import { work } from './foo.js';\nexport function run() { return work(); }\n"})
    get_sync_queue().put(("update", caller_path))
    index.drain_pending_events()
    assert index.client.get_callees("caller.ts:run") == ["foo.js:work"]

    os.remove(js_path)
    get_sync_queue().put(("delete", js_path))
    index.drain_pending_events()
    # NodeNext-style .js specifier falls back to the TS source when no JS file exists.
    assert index.client.get_callees("caller.ts:run") == ["foo.ts:work"]


def test_inline_handler_parameter_shadows_import(indexer):
    index, _ = indexer({
        "dep.ts": "export function work() { return true; }\n",
        "routes.ts": """
import { Router } from 'express';
import { work } from './dep';
const router = Router();
router.get('/', req => { const { work } = req; return work(); });
""",
    })
    handlers = [node_id for node_id, meta in index.client.get_all_metadata().items()
                if meta.get("type") == "Function" and "handler_get_L" in node_id]
    assert len(handlers) == 1
    assert index.client.get_node_meta(handlers[0])["javascript_shadowed_names"] == ["req", "work"]
    assert index.client.get_callees(handlers[0]) == []


def test_legacy_javascript_native_calls_are_quarantined(isolated_temp_dir):
    client = EngramClient(isolated_temp_dir)
    try:
        client.add_node("a.ts:work", "Function", "work", "a.ts")
        client.add_node("b.ts:work", "Function", "work", "b.ts")
        client.engine.add_node(
            node_id="caller.ts:run", node_type="Function", name="run",
            file_path="caller.ts", calls=["work"])
        client.engine.resolve_and_connect_calls("caller.ts:run", ["work"])
        client.build()
        assert set(client.get_callees("caller.ts:run")) == {"a.ts:work", "b.ts:work"}

        client.repopulate_edges()
        client.rebuild()
        assert client.get_callees("caller.ts:run") == []
        assert client.resolve_and_connect_calls("caller.ts:run", ["work"]) == []
        assert client.get_callees("caller.ts:run") == []
        assert "calls" not in client.engine.get_node_meta("caller.ts:run")
        assert client.get_node_meta("caller.ts:run")["calls"] == ["work"]
    finally:
        client.close()


# ── framework linking (routes, HTTP calls) ───────────────────────────────

class TestFullStack:
    def test_routes_link_views_and_frontend(self, indexer):
        index, structure = indexer(FULLSTACK)
        client = index.client
        routes = sorted(nid for nid, m in client.get_all_metadata().items() if m.get("type") == "Route")
        assert routes == ["urls.py:users/", "urls.py:users/<int:pk>/"]
        assert "views.py:user_list" in client.get_callees("urls.py:users/")
        assert "views.py:user_detail" in client.get_callees("urls.py:users/<int:pk>/")
        assert any("UsersPage" in c for c in client.get_callers("urls.py:users/"))
        # /api/users/123/ is the detail route; a name guess must not attach it to user_list.
        assert any("UserDetail" in c for c in client.get_callers("urls.py:users/<int:pk>/"))
        assert not any("UserDetail" in c for c in client.get_callers("views.py:user_list"))

        route = structure.lookup("urls.py:users/")
        assert route["symbol"]["kind"] == "Route" and route["symbol"]["handlers"] == ["views.user_list"]
        assert "views.py:user_list" in route["relationships"]["callees"]

    def test_blast_radius_from_view_reaches_frontend_by_depth(self, indexer):
        _, structure = indexer(FULLSTACK)
        deep = structure.impact("views.py:user_list", depth=3)
        assert deep["ok"]
        assert "urls.py" in deep["affected"] and "UsersPage.tsx" in deep["affected"]
        assert any("heuristic" in w for w in deep["meta"]["warnings"])

        shallow = structure.impact("views.py:user_list", depth=1)
        assert list(shallow["affected"]) == ["urls.py"]
        assert shallow["meta"]["more_beyond_depth"] is True

    def test_cross_file_ninja_router_mount_prefixes(self, indexer):
        _, structure = indexer({
            "src/modules/achat/api.py": """
from ninja import Router
router = Router()

@router.get("/credit-notes/{cn_id}")
def get_credit_note(request, cn_id: int):
    pass

@router.get("/orders")
def list_orders(request):
    pass
""",
            "src/modules/sales/api.py": """
from ninja import Router
router = Router(prefix="/sales")

@router.get("/customers")
def list_customers(request):
    pass
""",
            "a_main_app/urls.py": """
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
""",
        })
        achat = structure.map("src/modules/achat/api.py")
        urls = {s["url"] for s in achat["symbols"] if s["kind"] == "Route"}
        assert urls == {"/api/achat/credit-notes/{cn_id}", "/api/achat/orders"}
        orders = structure.lookup("src/modules/achat/api.py:/orders")
        assert orders["symbol"]["url"] == "/api/achat/orders"
        assert "src/modules/achat/api.py:list_orders" in orders["relationships"]["callees"]

    def test_mount_metadata_is_idempotent_and_survives_warm_start(self, indexer):
        files = {
            "shop/api.py": """
from ninja import Router
router = Router()

@router.get("/items")
def list_items(request):
    return []
""",
            "project/urls.py": """
from django.urls import path
from ninja import NinjaAPI
from shop.api import router as shop_router

api = NinjaAPI()
api.add_router("/v1/", shop_router)
urlpatterns = [path("api/", api.urls)]
""",
        }
        index, structure = indexer(files)
        route_id = "shop/api.py:/items"
        assert structure.lookup(route_id)["symbol"]["url"] == "/api/v1/items"

        index.resolve_cross_file_edges()
        index.client.rebuild()
        assert structure.lookup(route_id)["symbol"]["url"] == "/api/v1/items"

        _release(indexer.ws)
        restored = WorkspaceIndex(indexer.ws)
        summary = restored.initial_scan()
        assert summary["warm_start"] is True
        restored_structure = CodeStructure(restored.client, indexer.ws)
        assert restored_structure.lookup(route_id)["symbol"]["url"] == "/api/v1/items"
        assert "shop/api.py:list_items" in restored.client.get_callees(route_id)

        _write(indexer.ws, {"project/urls.py": files["project/urls.py"].replace('/v1/', '/v2/')})
        get_sync_queue().put(("update", os.path.join(indexer.ws, "project", "urls.py")))
        restored.drain_pending_events()
        assert restored_structure.lookup(route_id)["symbol"]["url"] == "/api/v2/items"

        without_mount = files["project/urls.py"].replace(
            'api.add_router("/v1/", shop_router)\n', '')
        _write(indexer.ws, {"project/urls.py": without_mount})
        get_sync_queue().put(("update", os.path.join(indexer.ws, "project", "urls.py")))
        restored.drain_pending_events()
        assert restored_structure.lookup(route_id)["symbol"]["url"] == "/items"

    def test_duplicate_view_names_link_to_the_route_in_the_same_app(self, indexer):
        index, _ = indexer({
            "a/views.py": "def index(request):\n    return 'a'\n",
            "a/urls.py": "from django.urls import path\nfrom . import views\nurlpatterns = [path('a/', views.index)]\n",
            "b/views.py": "def index(request):\n    return 'b'\n",
            "b/urls.py": "from django.urls import path\nfrom . import views\nurlpatterns = [path('b/', views.index)]\n",
        })
        assert index.client.get_callees("a/urls.py:a/") == ["a/views.py:index"]
        assert index.client.get_callees("b/urls.py:b/") == ["b/views.py:index"]

    def test_aliased_duplicate_view_names_use_import_context(self, indexer):
        index, _ = indexer({
            "a/views.py": "def index(request):\n    return 'a'\n",
            "b/views.py": "def index(request):\n    return 'b'\n",
            "urls.py": """
from django.urls import path
from a import views as av
from b import views as bv
urlpatterns = [path('a/', av.index), path('b/', bv.index)]
""",
        })
        assert index.client.get_callees("urls.py:a/") == ["a/views.py:index"]
        assert index.client.get_callees("urls.py:b/") == ["b/views.py:index"]

    def test_static_numeric_routes_do_not_collapse(self, isolated_temp_dir):
        client = EngramClient(isolated_temp_dir)
        try:
            client.add_node("routes.py:reports/2024/", "Route", "reports/2024/", "routes.py",
                            _extra={"url": "/reports/2024/"})
            client.add_node("routes.py:reports/2025/", "Route", "reports/2025/", "routes.py",
                            _extra={"url": "/reports/2025/"})
            client.add_node("page.ts:load2024", "Function", "load2024", "page.ts",
                            _extra={"http_calls": [{"url": "/reports/2024/", "method": "GET"}]})
            client.add_node("page.ts:load2025", "Function", "load2025", "page.ts",
                            _extra={"http_calls": [{"url": "/reports/2025/", "method": "GET"}]})
            client.add_node("page.ts:post2024", "Function", "post2024", "page.ts",
                            _extra={"http_calls": [{"url": "/reports/2024/", "method": "POST"}]})
            client.resolve_api_calls()
            client.build()
            assert client.get_callers("routes.py:reports/2024/") == ["page.ts:load2024"]
            assert client.get_callers("routes.py:reports/2025/") == ["page.ts:load2025"]
        finally:
            client.close()

    def test_duplicate_url_does_not_pick_an_arbitrary_route(self, isolated_temp_dir):
        client = EngramClient(isolated_temp_dir)
        try:
            for route_id in ("a/routes.py:users/", "b/routes.py:users/"):
                client.add_node(route_id, "Route", "users/", route_id.split(":", 1)[0],
                                _extra={"url": "/users/", "methods": ["GET"]})
            client.add_node("page.ts:load", "Function", "load", "page.ts",
                            _extra={"http_calls": [{"url": "/users/", "method": "GET"}]})
            client.resolve_api_calls()
            client.build()
            assert client.get_callers("a/routes.py:users/") == []
            assert client.get_callers("b/routes.py:users/") == []
        finally:
            client.close()

    def test_method_mismatch_does_not_fall_back_to_endpoint_name(self, isolated_temp_dir):
        client = EngramClient(isolated_temp_dir)
        try:
            client.add_node("backend.py:users", "Function", "users", "backend.py",
                            _extra={"api_endpoint": {"url": "/users", "methods": ["GET"]}})
            client.add_node("page.ts:submit", "Function", "submit", "page.ts",
                            _extra={"http_calls": [{"url": "/users", "method": "POST"}]})
            client.resolve_api_calls()
            client.build()
            assert client.get_callees("page.ts:submit") == []
        finally:
            client.close()

    def test_declarations_and_anonymous_handlers_are_indexed(self, indexer):
        _, structure = indexer({
            "db/schema.ts": """
import { pgTable, serial, text } from 'drizzle-orm/pg-core';

export const usersTable = pgTable('users', {
    id: serial('id').primaryKey(),
    name: text('name'),
});

const client = db.connect('prod');
""",
            "routes/rentals.ts": """
import { Router } from 'express';
import { db } from '@workspace/db';
const router = Router();
router.get('/rentals/:id/return', async (req, res) => {
  await db.update(rentalsTable).set({ status: 'returned' }).returning();
  res.json(rental);
});
export default router;
""",
        })
        decl = structure.lookup("usersTable")
        assert decl["status"] == "resolved"
        assert decl["symbol"]["kind"] == "Declaration"
        assert decl["symbol"]["initializer_call"] == "pgTable"
        assert decl["symbol"]["is_exported"] is True
        assert [s["name"] for s in structure.map("db/schema.ts")["symbols"]] == ["usersTable", "client"]

        route = structure.lookup("routes/rentals.ts:/rentals/:id/return")
        assert route["status"] == "resolved" and route["symbol"]["kind"] == "Route"
        handlers = [c for c in route["relationships"]["callees"] if "handler_get_L" in c]
        assert handlers, route
        handler = structure.lookup(handlers[0])
        assert handler["symbol"]["kind"] == "Function"
        assert "db.update" in handler["relationships"]["unresolved_calls"]

    def test_stdlib_imports_are_not_created_as_files(self, indexer):
        index, _ = indexer({"utils.py": "def helper(): pass\n"})
        handler = index.handler
        for module in ("dataclasses", "typing", "tkinter", "os", "pydantic"):
            assert handler._resolve_import_path(module, "app.py") is None
        assert handler._resolve_import_path("utils", "app.py") == "utils.py"


# ── persistence and lifecycle ────────────────────────────────────────────

def test_typed_edges_survive_snapshot_and_repopulation(isolated_temp_dir):
    ws = isolated_temp_dir
    _write(ws, {"caller.py": "def run():\n    return execute()\n",
                "service.py": "def execute():\n    return True\n"})
    caller, callee = "caller.py:run", "service.py:execute"
    client = EngramClient(ws)
    client.add_node("caller.py", "File", "caller.py", "caller.py")
    client.add_node(caller, "Function", "run", "caller.py", calls=["execute"], lines={"start": 1, "end": 2})
    client.add_node(callee, "Function", "execute", "service.py", lines={"start": 1, "end": 2})
    client.add_structural_edge(caller, "caller.py")
    client.resolve_and_connect_calls(caller, ["execute"])
    client.build()

    assert client.get_callees(caller) == [callee]
    assert set(client.get_dependencies(caller)) == {"caller.py", callee}
    rel = CodeStructure(client, ws).lookup(caller)["relationships"]
    assert rel["callees"] == [callee] and "related" not in rel
    client.close()

    restored = EngramClient(ws)
    try:
        assert restored.get_callees(caller) == [callee]
        assert set(restored.get_dependencies(caller)) == {"caller.py", callee}
        restored.repopulate_edges()
        restored.build()
        assert restored.get_callees(caller) == [callee]
        assert CodeStructure(restored, ws).impact(callee)["direct_callers"] == [caller]
    finally:
        restored.close()


def test_warm_start_is_used_only_when_nothing_changed(indexer):
    files = {"a.py": "def alpha():\n    return 1\n", "b.py": "from a import alpha\n\ndef beta():\n    return alpha()\n"}
    index, _ = indexer(files)
    first_nodes = index.node_count

    # Same files on disk -> fresh process restores the snapshot and skips parsing.
    _release(indexer.ws)
    second = WorkspaceIndex(indexer.ws)
    summary = second.initial_scan()
    assert summary["warm_start"] is True
    assert summary["nodes"] == first_nodes
    assert CodeStructure(second.client, indexer.ws).impact("alpha")["direct_callers"] == ["b.py:beta"]

    # A changed file invalidates the warm start and the new symbol is indexed.
    _write(indexer.ws, {"a.py": "def alpha():\n    return 1\n\ndef gamma():\n    return 2\n"})
    _release(indexer.ws)
    third = WorkspaceIndex(indexer.ws)
    summary = third.initial_scan()
    assert summary["warm_start"] is False
    assert CodeStructure(third.client, indexer.ws).lookup("gamma")["status"] == "resolved"


def test_warm_start_rejects_manifest_newer_than_snapshot(indexer):
    index, _ = indexer({"a.py": "def alpha():\n    return 1\n"})
    _release(indexer.ws)

    _write(indexer.ws, {"a.py": "def gamma():\n    return 2\n"})
    meta_path = os.path.join(indexer.ws, INDEX_META_FILENAME)
    with open(meta_path) as fh:
        meta = json.load(fh)
    stat = os.stat(os.path.join(indexer.ws, "a.py"))
    # Simulate a process dying after publishing its sidecar but before a newer
    # asynchronous snapshot reached disk.
    meta["file_manifest"]["a.py"] = [stat.st_mtime_ns, stat.st_size]
    with open(meta_path, "w") as fh:
        json.dump(meta, fh)

    restored = WorkspaceIndex(indexer.ws)
    summary = restored.initial_scan()
    assert summary["warm_start"] is False
    structure = CodeStructure(restored.client, indexer.ws)
    assert structure.lookup("gamma")["status"] == "resolved"
    assert structure.lookup("alpha")["status"] == "not_found"


def test_failed_initial_file_is_not_accepted_by_next_warm_start(isolated_temp_dir, monkeypatch):
    from src.database.parser.ast_parser import UniversalCodeParser

    ws = isolated_temp_dir
    _write(ws, {"good.py": "def good(): pass\n", "bad.py": "def recovered(): pass\n"})
    monkeypatch.setenv("WORKSPACE_PATH", ws)
    original = UniversalCodeParser.parse_file

    with monkeypatch.context() as patch:
        def fail_one(self, path):
            if path.endswith("bad.py"):
                raise RuntimeError("temporary parser failure")
            return original(self, path)

        patch.setattr(UniversalCodeParser, "parse_file", fail_one)
        first = WorkspaceIndex(ws)
        first.initial_scan()
        assert first.sync_errors == {"bad.py"}
        assert CodeStructure(first.client, ws).lookup("recovered")["status"] == "not_found"
        saved = first.client.load_index_meta()
        assert set(saved["file_manifest"]) == {"good.py"}
        _release(ws)

    second = WorkspaceIndex(ws)
    summary = second.initial_scan()
    assert summary["warm_start"] is False
    assert CodeStructure(second.client, ws).lookup("recovered")["status"] == "resolved"


def test_index_stale_flag_surfaces_in_tool_meta(indexer):
    index, structure = indexer({"a.py": "def alpha():\n    return 1\n"})
    assert structure.map(".")["meta"]["index_stale"] is False

    meta_path = os.path.join(indexer.ws, INDEX_META_FILENAME)
    with open(meta_path) as fh:
        meta = json.load(fh)
    meta["fingerprint"] = "0000deadbeef0000"
    with open(meta_path, "w") as fh:
        json.dump(meta, fh)

    out = structure.lookup("alpha")
    assert out["meta"]["index_stale"] is True
    assert any("rescan" in w for w in out["meta"]["warnings"])


def test_drain_applies_events_and_reports_failures(indexer):
    index, structure = indexer({"a.py": "def alpha():\n    return 1\n"})
    assert structure.lookup("new_symbol")["status"] == "not_found"

    new_file = os.path.join(indexer.ws, "new.py")
    _write(indexer.ws, {"new.py": "from a import alpha\n\ndef new_symbol():\n    return alpha()\n"})
    get_sync_queue().put(("update", new_file))
    assert index.drain_pending_events() == 1
    assert index.sync_errors == set()
    assert structure.lookup("new_symbol")["status"] == "resolved"
    assert structure.impact("alpha")["direct_callers"] == ["new.py:new_symbol"]

    # Deleting the file removes its symbols and the caller edge.
    os.remove(new_file)
    get_sync_queue().put(("delete", new_file))
    index.drain_pending_events()
    assert structure.lookup("new_symbol")["status"] == "not_found"
    assert structure.impact("alpha")["meta"]["direct"] == 0

    # A failing update is reported in every subsequent response, not swallowed.
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")
    index.handler.update_file_in_graph = boom
    get_sync_queue().put(("update", os.path.join(indexer.ws, "a.py")))
    index.drain_pending_events()
    assert index.sync_errors == {"a.py"}
    warnings = structure.map(".")["meta"]["warnings"]
    assert any("a.py" in w and "stale" in w for w in warnings)


def test_parse_failure_preserves_last_known_good_file(indexer, monkeypatch):
    index, structure = indexer({"a.py": "def alpha():\n    return 1\n"})
    path = os.path.join(indexer.ws, "a.py")
    _write(indexer.ws, {"a.py": "def gamma():\n    return 2\n"})

    def fail_parse(_path):
        raise RuntimeError("temporary grammar failure")

    monkeypatch.setattr(index.handler.parser, "parse_file", fail_parse)
    get_sync_queue().put(("update", path))
    index.drain_pending_events()

    assert structure.lookup("alpha")["status"] == "resolved"
    assert structure.lookup("gamma")["status"] == "not_found"
    assert index.sync_errors == {"a.py"}
    assert any("a.py" in w for w in structure.map(".")["meta"]["warnings"])


def test_watcher_and_scan_share_env_exclusion_policy(isolated_temp_dir):
    from src.watcher.sync_handler import GraphSyncHandler

    ordinary = os.path.join(isolated_temp_dir, "env")
    os.makedirs(ordinary)
    source = os.path.join(ordinary, "source.py")
    with open(source, "w") as fh:
        fh.write("def visible(): pass\n")

    handler = GraphSyncHandler(isolated_temp_dir)
    assert handler._is_excluded(source) is False
    assert handler._is_excluded(os.path.join(isolated_temp_dir, INDEX_META_FILENAME)) is True

    declaration = os.path.join(isolated_temp_dir, "types.d.ts")
    regular_ts = os.path.join(isolated_temp_dir, "app.ts")
    with open(declaration, "w") as fh:
        fh.write("export interface User { id: number }\n")
    with open(regular_ts, "w") as fh:
        fh.write("export function run() {}\n")
    assert handler._is_excluded(declaration) is True

    with open(os.path.join(ordinary, "pyvenv.cfg"), "w") as fh:
        fh.write("home = /python\n")
    assert handler._is_excluded(source) is True

    index = WorkspaceIndex(isolated_temp_dir)
    try:
        collected = {os.path.relpath(path, isolated_temp_dir)
                     for path in index.collect_source_files()}
        assert "app.ts" in collected
        assert "types.d.ts" not in collected
        assert os.path.join("env", "source.py") not in collected
    finally:
        _release(isolated_temp_dir)


def test_failed_graph_rebuild_requeues_events(indexer, monkeypatch):
    index, _ = indexer({"a.py": "def alpha(): pass\n"})
    path = os.path.join(indexer.ws, "a.py")
    _write(indexer.ws, {"a.py": "def alpha():\n    return 2\n"})
    original = index.resolve_cross_file_edges
    attempts = {"count": 0}

    def fail_once():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary relink failure")
        return original()

    monkeypatch.setattr(index, "resolve_cross_file_edges", fail_once)
    get_sync_queue().put(("update", path))
    with pytest.raises(RuntimeError, match="temporary relink failure"):
        index.drain_pending_events()
    assert "<graph-rebuild>" in index.sync_errors
    assert index._retry_events == [("update", path)]
    assert index.client.is_index_stale() is True

    assert index.drain_pending_events() == 1
    assert "<graph-rebuild>" not in index.sync_errors
    assert index.client.is_index_stale() is False


def test_partial_graph_is_never_accepted_as_warm_snapshot(indexer, monkeypatch):
    index, _ = indexer(FULLSTACK)
    assert "views.py:user_list" in index.client.get_callees("urls.py:users/")
    path = os.path.join(indexer.ws, "views.py")

    def fail_after_generated_edges_were_cleared():
        raise RuntimeError("relink interrupted")

    monkeypatch.setattr(index.client, "repopulate_edges", fail_after_generated_edges_were_cleared)
    get_sync_queue().put(("update", path))
    with pytest.raises(RuntimeError, match="relink interrupted"):
        index.drain_pending_events()
    assert index.client.is_index_stale() is True
    _release(indexer.ws)  # close() persists the partial engine, but the dirty marker remains

    restored = WorkspaceIndex(indexer.ws)
    summary = restored.initial_scan()
    assert summary["warm_start"] is False
    assert "views.py:user_list" in restored.client.get_callees("urls.py:users/")


def test_interrupt_marks_graph_dirty_before_mutation(indexer, monkeypatch):
    index, _ = indexer(FULLSTACK)
    path = os.path.join(indexer.ws, "views.py")

    def interrupt():
        raise KeyboardInterrupt()

    monkeypatch.setattr(index.client, "repopulate_edges", interrupt)
    get_sync_queue().put(("update", path))
    with pytest.raises(KeyboardInterrupt):
        index.drain_pending_events()
    assert index.client.is_index_stale() is True


def test_invalidate_file_does_not_remove_similar_extension_metadata(isolated_temp_dir):
    client = EngramClient(isolated_temp_dir)
    try:
        client.add_node("a.ts", "File", "a.ts", "a.ts", _extra={"marker": "ts"})
        client.add_node("a.tsx", "File", "a.tsx", "a.tsx", _extra={"marker": "tsx"})
        client.invalidate_file("a.ts")
        assert client.get_node_meta("a.ts") is None
        assert client.get_node_meta("a.tsx")["marker"] == "tsx"
    finally:
        client.close()


def test_malformed_index_meta_is_stale_not_an_exception(isolated_temp_dir):
    client = EngramClient(isolated_temp_dir)
    try:
        with open(os.path.join(isolated_temp_dir, INDEX_META_FILENAME), "w") as fh:
            json.dump([], fh)
        assert client.is_index_stale() is True
    finally:
        client.close()


def test_code_map_on_real_workspace(indexer):
    _, structure = indexer({
        "app/models.py": "class User:\n    def name(self):\n        return 'u'\n",
        "app/views.py": "from .models import User\n\ndef index(request):\n    return User()\n",
        "README.md": "# hello\n",
    })
    root = structure.map(".")
    assert root["meta"]["kind"] == "directory"
    assert [f["path"] for f in root["folders"]] == ["app"]
    assert root["folders"][0]["files_count"] == 2
    assert [f["path"] for f in root["files"]] == ["README.md"]

    app = structure.map("app")
    by_path = {f["path"]: f for f in app["files"]}
    assert by_path["app/models.py"]["symbols"] == {"Class": 1, "Function": 1}
    assert by_path["app/models.py"]["top_level"] == "User"
    assert by_path["app/views.py"]["top_level"] == "index"

    models = structure.map("app/models.py")
    assert models["symbols"][0]["name"] == "User"
    assert models["symbols"][0]["members"][0]["name"] == "name"
    assert models["symbols"][0]["members"][0]["lines"] == "2-3"


def test_possible_callers_recover_untyped_receiver_calls_without_the_decoy(indexer):
    """Real parse of the pattern that hides callers: ``self.app.router.add_route``
    where ``app`` is an unannotated parameter (no resolver can type it), next to
    another class defining its own ``add_route`` that resolves normally."""
    _, structure = indexer({
        "fx/__init__.py": "",
        "fx/routing.py": (
            "class Router:\n"
            "    def add_route(self, path, handler, methods=None, name=None):\n"
            "        return (path, handler)\n\n"
            "    def get(self, path, name=None):\n"
            "        def decorator(handler):\n"
            "            self.add_route(path, handler, ['GET'], name)\n"
            "            return handler\n"
            "        return decorator\n"
        ),
        "fx/app.py": (
            "from fx.routing import Router\n\n"
            "class Application:\n"
            "    def __init__(self):\n"
            "        self.router = Router()\n\n"
            "    def add_docs(self):\n"
            "        def docs(request):\n"
            "            return 'docs'\n"
            "        self.router.add_route('/docs', docs, ['GET'])\n"
        ),
        "fx/admin.py": (
            "class AdminDashboard:\n"
            "    def __init__(self, app, prefix='/admin'):\n"
            "        self.app = app\n"
            "        self.prefix = prefix\n\n"
            "    def _register_routes(self):\n"
            "        def index(request):\n"
            "            return 'admin'\n"
            "        self.app.router.add_route(self.prefix, index, ['GET'])\n"
            "        self.app.router.add_route(self.prefix + '/api', index, ['GET'])\n"
        ),
        "fx/websocket.py": (
            "class WebSocketRouter:\n"
            "    def route(self, path):\n"
            "        def decorator(handler):\n"
            "            self.add_route(path, handler)\n"
            "            return handler\n"
            "        return decorator\n\n"
            "    def add_route(self, path, handler):\n"
            "        return (path, handler)\n"
        ),
    })
    out = structure.impact("Router.add_route", depth=3)
    assert out["ok"] and out["target"]["id"] == "fx/routing.py:Router.add_route"
    confirmed = {entry.split(" ")[0] for entries in out["affected"].values() for entry in entries}
    assert confirmed == {"Router.get.decorator", "Application.add_docs"}
    assert "fx/websocket.py" not in out["affected"]
    assert out["meta"]["possible"] == 1
    assert out["possible_callers"] == [
        {"id": "fx/admin.py:AdminDashboard._register_routes", "via": "self.app.router.add_route"},
    ]
    assert any("possible_callers" in w for w in out["meta"]["warnings"])

    rel = structure.lookup("Router.add_route")["relationships"]
    assert rel["possible_callers"][0]["id"] == "fx/admin.py:AdminDashboard._register_routes"
    # The decoy keeps its own resolved caller. The untyped call is by definition a
    # candidate for EVERY same-named method, so it is listed here too: the graph
    # states what it cannot decide instead of guessing a receiver type.
    decoy = structure.lookup("WebSocketRouter.add_route")["relationships"]
    assert decoy["callers"] == ["fx/websocket.py:WebSocketRouter.route.decorator"]
    assert decoy["possible_callers"] == rel["possible_callers"]
