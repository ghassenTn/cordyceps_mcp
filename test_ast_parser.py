import pytest
import os
import tempfile


pytestmark = pytest.mark.unit


@pytest.fixture
def parser():
    from src.database.parser.ast_parser import UniversalCodeParser
    return UniversalCodeParser()


def _write_py_file(tmpdir, name, content):
    path = os.path.join(tmpdir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)
    return path


def _write_js_file(tmpdir, name, content):
    path = os.path.join(tmpdir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)
    return path


class TestPythonParsing:
    def test_parse_simple_function(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "mod.py", """
def greet(name):
    return f"Hello {name}"
""")
        result = parser.parse_file(path)
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "greet"
        assert result["functions"][0]["lines"]["start"] == 2
        assert result["functions"][0]["lines"]["end"] == 3

    def test_parse_class_with_methods(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "models.py", """
class User:
    def __init__(self, name):
        self.name = name

    def say_hello(self):
        return f"Hi {self.name}"

class Admin(User):
    pass
""")
        result = parser.parse_file(path)
        assert len(result["classes"]) == 2
        class_names = {c["name"] for c in result["classes"]}
        assert class_names == {"User", "Admin"}

        user = [c for c in result["classes"] if c["name"] == "User"][0]
        assert len(user["methods"]) == 2
        assert user["methods"][0]["name"] == "__init__"
        assert user["methods"][1]["name"] == "say_hello"

    def test_parse_decorated_function(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "views.py", """
@api_view(['GET'])
def list_users(request):
    return Response({"users": []})
""")
        result = parser.parse_file(path)
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "list_users"
        assert len(result["endpoints"]) == 1
        assert result["endpoints"][0]["function"] == "list_users"

    def test_parse_imports(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "app.py", """
import os
import sys
from datetime import datetime
from collections.abc import Iterator
""")
        result = parser.parse_file(path)
        imports = set(result["imports"])
        assert "os" in imports
        assert "sys" in imports
        assert "datetime" in imports
        assert "collections.abc" in imports or "collections" in imports

    def test_parse_calls(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "service.py", """
def do_work():
    result = fetch_data()
    processed = transform(result)
    return processed
""")
        result = parser.parse_file(path)
        calls = result["functions"][0]["calls"]
        assert "fetch_data" in calls
        assert "transform" in calls

    def test_parse_returns(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "calc.py", """
def compute(a, b):
    if a > b:
        return a
    return b
""")
        result = parser.parse_file(path)
        returns = result["functions"][0]["returns"]
        assert len(returns) == 2

    def test_parse_docstring(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "util.py", """
def load_config():
    \"\"\"Load the application configuration from the settings file.\"\"\"
    return {}
""")
        result = parser.parse_file(path)
        assert result["functions"][0]["docstring"] == "Load the application configuration from the settings file."

    def test_parse_django_endpoint(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "api.py", """
from rest_framework.decorators import api_view

@api_view(['GET', 'POST'])
def article_list(request):
    pass
""")
        result = parser.parse_file(path)
        assert len(result["endpoints"]) == 1
        ep = result["endpoints"][0]
        assert ep["function"] == "article_list"
        assert "GET" in ep["methods"]
        assert "POST" in ep["methods"]
        assert ep["framework"] == "django"

    def test_parse_fastapi_endpoint(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "fast.py", """
from fastapi import APIRouter
router = APIRouter()

@router.get("/items/{item_id}")
def get_item(item_id: int):
    return {"item_id": item_id}
""")
        result = parser.parse_file(path)
        assert len(result["endpoints"]) == 1
        ep = result["endpoints"][0]
        assert ep["function"] == "get_item"
        assert "GET" in ep["methods"]
        assert ep["framework"] == "fastapi"

    def test_parse_flask_endpoint(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "flask_app.py", """
from flask import Flask
app = Flask(__name__)

@app.route('/hello', methods=['GET'])
def hello():
    return "Hello"
""")
        result = parser.parse_file(path)
        assert len(result["endpoints"]) == 1
        ep = result["endpoints"][0]
        assert ep["function"] == "hello"
        assert "GET" in ep["methods"]
        assert ep["framework"] == "flask"

    def test_parse_http_calls(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "client.py", """
import requests

def get_users():
    response = requests.get('https://api.example.com/users')
    return response.json()
""")
        result = parser.parse_file(path)
        assert "requests" in result["imports"]
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "get_users"

    def test_parse_async_function(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "async_mod.py", """
async def fetch(url):
    return await http.get(url)
""")
        result = parser.parse_file(path)
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "fetch"

    def test_parse_model_class(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "models.py", """
from django.db import models

class Product(models.Model):
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return self.name
""")
        result = parser.parse_file(path)
        assert len(result["classes"]) == 1
        assert result["classes"][0]["name"] == "Product"
        assert len(result["classes"][0]["methods"]) >= 1

    def test_parse_empty_file(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "empty.py", "")
        result = parser.parse_file(path)
        assert result["functions"] == []
        assert result["classes"] == []

    def test_nested_function_closures_are_indexed(self, parser, isolated_temp_dir):
        """Closures, defs inside if blocks, and decorator-wrapped helpers become
        first-class indexed nodes with qualified node_name."""
        path = _write_py_file(isolated_temp_dir, "nested.py", """
import json


def outer(x):
    factor = 2

    @wrap
    def multiply(y):
        return y * factor

    def inner(z):
        def deepest(w):
            return w + 1
        return deepest(z)

    if x > 0:
        def conditional_helper():
            return "pos"
    return multiply(x)
""")
        result = parser.parse_file(path)
        names = {f["node_name"] for f in result["functions"]}
        assert "outer" in names
        assert "outer.multiply" in names
        assert "outer.inner" in names
        assert "outer.inner.deepest" in names
        assert "outer.conditional_helper" in names
        # Bare names must be preserved alongside the qualified id.
        by_node = {f["node_name"]: f for f in result["functions"]}
        assert by_node["outer.multiply"]["name"] == "multiply"
        assert by_node["outer.multiply"]["decorators"] == ["wrap"]
        assert by_node["outer.inner"]["calls"] == ["deepest"]
        assert by_node["outer"]["calls"] == ["multiply"]

    def test_calls_exclude_decorators_defaults_and_nested_bodies(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "ownership.py", """
@decorate(factory())
def parent(value=default_factory()):
    def child():
        return nested_only()
    return direct_call()
""")
        result = parser.parse_file(path)
        by_node = {f["node_name"]: f for f in result["functions"]}

        assert by_node["parent"]["calls"] == ["direct_call"]
        assert by_node["parent.child"]["calls"] == ["nested_only"]

    def test_super_call_keeps_receiver(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "inheritance.py", """
class Base:
    def run(self):
        return True

class Child(Base):
    def run(self):
        return super().run()
""")
        result = parser.parse_file(path)
        child = next(c for c in result["classes"] if c["name"] == "Child")
        assert "super.run" in child["methods"][0]["calls"]

    def test_python_import_bindings_preserve_aliases_and_scope(self):
        from src.watcher.sync_handler import _python_import_bindings

        bindings = _python_import_bindings("""
from pkg.a import work as run
import pkg.b as svc
import pkg.a
import pkg.b

def outer():
    from .local import helper as local_helper
    def inner():
        import pkg.deep as deep
        return deep.execute(local_helper())
""", "src/app/caller.py")

        assert bindings[""]["run"] == {
            "module": "pkg/a", "symbol": "work", "kind": "from"
        }
        assert bindings[""]["svc"] == {
            "module": "pkg/b", "symbol": None, "kind": "module"
        }
        assert bindings["outer"]["local_helper"]["module"] == "src/app/local"
        assert bindings["outer.inner"]["deep"]["module"] == "pkg/deep"
        assert bindings[""]["pkg"] == [
            {"module": "pkg/a", "symbol": None, "kind": "module", "qualifier": "pkg.a"},
            {"module": "pkg/b", "symbol": None, "kind": "module", "qualifier": "pkg.b"},
        ]

    def test_python_receiver_types_track_annotations_and_assignments(self):
        from src.watcher.sync_handler import _python_receiver_types

        receivers = _python_receiver_types("""
class Service:
    repository: Repository

    def __init__(self, repo: Repository):
        self.repo = repo

    def run(self, worker: Worker):
        local = Handler()
        worker.execute()
        local.handle()
        self.repo.save()

    def forward(self, repo: "Repository | None"):
        return repo.save()

    def ambiguous(self, repo: Repository | AlternateRepository):
        return repo.save()
""")

        assert receivers["Service"]["self.repository"] == "Repository"
        assert receivers["Service"]["self.repo"] == "Repository"
        assert receivers["Service.run"]["worker"] == "Worker"
        assert receivers["Service.run"]["local"] == "Handler"
        assert receivers["Service.forward"]["repo"] == "Repository"
        assert receivers["Service.ambiguous"]["repo"] is None

    def test_python_shadowed_names_track_local_bindings(self):
        from src.watcher.sync_handler import _python_shadowed_names

        shadows = _python_shadowed_names("""
value = factory()

def outer(work, typed: Worker):
    local = build()
    for item in values:
        pass
    def inner(work):
        return work()
""")

        assert "value" in shadows[""]
        assert {"work", "typed", "local", "item"} <= set(shadows["outer"])
        assert "work" in shadows["outer.inner"]

    def test_inner_schema_class_is_indexed(self, parser, isolated_temp_dir):
        """Inner schema classes (and classes nested inside methods) are indexed
        with qualified paths, including their own methods."""
        path = _write_py_file(isolated_temp_dir, "schema.py", """
class Publisher:
    def publish(self, msg):
        def write():
            class LineFormatter:
                def fmt(self, line):
                    return str(line)
            return LineFormatter()
        return write()


def make_factory():
    class InnerSchema:
        def render(self):
            return "rendered"
    return InnerSchema
""")
        result = parser.parse_file(path)
        cls_names = {c["node_name"] for c in result["classes"]}
        assert "Publisher" in cls_names
        assert "make_factory.InnerSchema" in cls_names
        assert "Publisher.publish.write.LineFormatter" in cls_names

        by_node = {c["node_name"]: c for c in result["classes"]}
        inner = by_node["make_factory.InnerSchema"]
        assert [m["node_name"] for m in inner["methods"]] == ["make_factory.InnerSchema.render"]
        assert "Publisher.publish.write.LineFormatter.fmt" in {
            m["node_name"] for m in by_node["Publisher.publish.write.LineFormatter"]["methods"]
        }

    def test_parse_nonexistent_file(self, parser):
        from src.database.parser.ast_parser import UniversalCodeParser
        with pytest.raises(FileNotFoundError):
            parser.parse_file("/nonexistent/path.py")

    def test_parse_unsupported_extension(self, parser, isolated_temp_dir):
        path = os.path.join(isolated_temp_dir, "data.csv")
        with open(path, 'w') as f:
            f.write("a,b,c\n1,2,3")
        with pytest.raises(ValueError, match="Unsupported file extension"):
            parser.parse_file(path)


class TestJSParsing:
    def test_parse_js_function(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "util.js", """
function greet(name) {
    return `Hello ${name}`;
}
""")
        result = parser.parse_file(path)
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "greet"

    def test_parse_js_class(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "models.js", """
class User {
    constructor(name) {
        this.name = name;
    }
    sayHello() {
        return `Hi ${this.name}`;
    }
}
""")
        result = parser.parse_file(path)
        assert len(result["classes"]) == 1
        assert result["classes"][0]["name"] == "User"
        methods = result["classes"][0]["methods"]
        assert len(methods) == 2
        assert methods[0]["name"] == "constructor"
        assert methods[1]["name"] == "sayHello"

    def test_parse_arrow_function(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "arrows.js", """
const add = (a, b) => a + b;
const greet = name => `Hello ${name}`;
""")
        result = parser.parse_file(path)
        assert len(result["functions"]) == 2
        names = {f["name"] for f in result["functions"]}
        assert "add" in names
        assert "greet" in names

    def test_parse_js_imports(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "app.js", """
import React from 'react';
import { useState } from 'react';
const fs = require('fs');
""")
        result = parser.parse_file(path)
        imports = set(result["imports"])
        assert "react" in imports

    def test_parse_js_exports(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "export_mod.js", """
export function hello() { return "hello"; }
export class User {}
const secret = "hidden";
export default secret;
""")
        result = parser.parse_file(path)
        assert len(result["exports"]) >= 2
        all_names = []
        for exp in result["exports"]:
            for n in exp["names"]:
                all_names.append(n["name"])
        assert "hello" in all_names
        assert "User" in all_names

    def test_parse_fetch_http_call(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "fetch_client.js", """
async function loadUsers() {
    const res = await fetch('/api/users');
    return res.json();
}
""")
        result = parser.parse_file(path)
        assert len(result["functions"]) == 1
        http_calls = result["http_calls"]
        assert len(http_calls) >= 1
        assert http_calls[0]["url"] == "/api/users"
        assert http_calls[0]["method"] == "GET"

    def test_parse_axios_http_call(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "api_client.js", """
import axios from 'axios';

async function saveUser(data) {
    const res = await axios.post('/api/users', data);
    return res.data;
}
""")
        result = parser.parse_file(path)
        http_calls = result["http_calls"]
        assert len(http_calls) >= 1
        assert http_calls[0]["url"] == "/api/users"
        assert http_calls[0]["method"] == "POST"
        assert http_calls[0]["lib"] == "axios"


class TestTSParsing:
    def test_parse_ts_function(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "utils.ts", """
function greet(name: string): string {
    return `Hello ${name}`;
}
""")
        result = parser.parse_file(path)
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "greet"
        assert "string" in result["functions"][0]["signature"]

    def test_nested_ts_arrows_and_functions(self, parser, isolated_temp_dir):
        """Nested arrows, inner functions and inner classes in TS are indexed
        with dotted qualified node_names."""
        path = _write_js_file(isolated_temp_dir, "nested.ts", """
export function makeHandler(prefix: string) {
  const normalize = (s: string) => s.trim();
  function inner() {
    class Bucket {
      add(x: number) {
        const plus = (y: number) => x + y;
        return plus(1);
      }
    }
    return new Bucket();
  }
  return { normalize, inner };
}
""")
        result = parser.parse_file(path)
        fn_names = {f["node_name"] for f in result["functions"]}
        assert "makeHandler" in fn_names
        assert "makeHandler.normalize" in fn_names
        assert "makeHandler.inner" in fn_names
        assert "makeHandler.inner.Bucket.add.plus" in fn_names

        cls_names = {c["node_name"] for c in result["classes"]}
        assert "makeHandler.inner.Bucket" in cls_names

    def test_parse_tsx_component(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "Button.tsx", """
import React from 'react';

interface Props {
    label: string;
}

export const Button: React.FC<Props> = ({ label }) => {
    return <button>{label}</button>;
};
""")
        result = parser.parse_file(path)
        functions = result["functions"]
        if functions:
            assert any(f["name"] == "Button" for f in functions)
        imports = set(result["imports"])
        assert "react" in imports


    def test_parse_framework_detection(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "component.jsx", """
import React from 'react';
function App() {
    return <div>Hello</div>;
}
export default App;
""")
        result = parser.parse_file(path)
        assert "react" in result["frameworks"]

    def test_parse_pytest_detection(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "test_demo.py", """
import pytest

def test_hello():
    assert 1 + 1 == 2
""")
        result = parser.parse_file(path)
        assert "pytest" in result["frameworks"]


class TestTSInheritance:
    def test_extends_and_implements_strip_keywords(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "classes.ts", """
interface IFace {
    run(): void;
}

class Base {
    base() { return 'base'; }
}

export class Child extends Base {
    child() { return 'child'; }
}

export class Impl implements IFace {
    run() { return 'run'; }
}

export class Impl2 implements IFace, Other {
    run() { return 'run'; }
}
""")
        result = parser.parse_file(path)
        by_name = {c["name"]: c.get("base_classes", []) for c in result["classes"]}
        assert by_name["Child"] == ["Base"]
        assert by_name["Impl"] == ["IFace"]
        assert by_name["Impl2"] == ["IFace", "Other"]
        assert by_name["Base"] == []


class TestQualifiedCalls:
    def test_bare_call(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "bare.py", """
def foo():
    bar()
""")
        result = parser.parse_file(path)
        assert result["functions"][0]["calls"] == ["bar"]

    def test_stdlib_qualified_call(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "stdlib_call.py", """
def foo():
    return re.search(r'pattern', text)
""")
        result = parser.parse_file(path)
        calls = result["functions"][0]["calls"]
        assert "re.search" in calls
        assert "search" not in calls

    def test_chained_qualified_call(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "chained.py", """
def foo():
    return db.client.search(keyword)
""")
        result = parser.parse_file(path)
        calls = result["functions"][0]["calls"]
        assert "db.client.search" in calls
        assert "search" not in calls

    def test_multiple_qualified_calls(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "multi.py", """
import os, json

def process():
    data = json.loads(text)
    path = os.path.join(root, name)
    return transform(data, path)
""")
        result = parser.parse_file(path)
        calls = result["functions"][0]["calls"]
        assert "json.loads" in calls
        assert "os.path.join" in calls
        assert "transform" in calls
        assert "loads" not in calls
        assert "join" not in calls

    def test_self_method_call(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "self_call.py", """
class Worker:
    def run(self):
        return self._process(data)
""")
        result = parser.parse_file(path)
        calls = result["classes"][0]["methods"][0]["calls"]
        assert "self._process" in calls
        assert "_process" not in calls


    def test_js_chained_call(self, parser, isolated_temp_dir):
        path = _write_js_file(isolated_temp_dir, "chain.js", """
function load() {
    return api.client.get('/users');
}
""")
        result = parser.parse_file(path)
        calls = result["functions"][0]["calls"]
        assert "api.client.get" in calls
        assert "get" not in calls

    def test_mixed_bare_and_qualified(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "mixed.py", """
def handler():
    data = fetch_input()
    result = re.match(pattern, data)
    return transform(result)
""")
        result = parser.parse_file(path)
        calls = result["functions"][0]["calls"]
        assert "re.match" in calls
        assert "fetch_input" in calls
        assert "transform" in calls


class TestURLPatterns:
    def test_extract_basic_path(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "urls.py", """
from django.urls import path
from . import views

urlpatterns = [
    path('users/', views.user_list, name='user-list'),
    path('users/<int:pk>/', views.user_detail, name='user-detail'),
]
""")
        result = parser.parse_file(path)
        patterns = result["url_patterns"]
        assert len(patterns) == 2
        assert patterns[0]["url"] == "users/"
        assert patterns[0]["view_name"] == "views.user_list"
        assert patterns[0]["name"] == "user-list"
        assert patterns[0]["func"] == "path"
        assert patterns[0]["is_include"] is False
        assert patterns[1]["url"] == "users/<int:pk>/"
        assert patterns[1]["view_name"] == "views.user_detail"
        assert patterns[1]["name"] == "user-detail"

    def test_extract_re_path(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "urls_re.py", """
from django.urls import re_path
from . import views

urlpatterns = [
    re_path(r'^search/$', views.search, name='search'),
]
""")
        result = parser.parse_file(path)
        patterns = result["url_patterns"]
        assert len(patterns) == 1
        assert patterns[0]["url"] == r'^search/$'
        assert patterns[0]["func"] == "re_path"

    def test_extract_include_skipped(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "urls_include.py", """
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
]
""")
        result = parser.parse_file(path)
        patterns = result["url_patterns"]
        assert len(patterns) == 2
        # admin.site.urls → not a path() target, view_name may be empty
        assert patterns[0]["url"] == "admin/"
        # include() → is_include=True
        assert patterns[1]["url"] == "api/"
        assert patterns[1]["is_include"] is True
        assert "include:" in patterns[1].get("view_name", "")

    def test_extract_ninja_router_endpoint(self, parser, isolated_temp_dir):
        """Detect django-ninja endpoint with custom router variable name."""
        path = _write_py_file(isolated_temp_dir, "items_router.py", """
from ninja import Router
items_router = Router()

@items_router.get("/list")
def list_items(request):
    pass

@items_router.post("/create")
def create_item(request):
    pass
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        assert len(endpoints) == 2
        eps_by_func = {ep["function"]: ep for ep in endpoints}
        assert "list_items" in eps_by_func
        assert eps_by_func["list_items"]["methods"] == ["GET"]
        assert eps_by_func["list_items"]["url"] == "/list"
        assert "create_item" in eps_by_func
        assert eps_by_func["create_item"]["methods"] == ["POST"]
        assert eps_by_func["create_item"]["url"] == "/create"

    def test_extract_ninja_api_endpoint(self, parser, isolated_temp_dir):
        """Detect django-ninja endpoint with custom API variable name."""
        path = _write_py_file(isolated_temp_dir, "api.py", """
from ninja import NinjaAPI
my_api = NinjaAPI()

@my_api.get("/hello")
def hello(request):
    pass

@my_api.api_operation(["GET", "POST"], url="/items/{id}")
def handle_item(request, id: int):
    pass
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        assert len(endpoints) == 2
        eps_by_func = {ep["function"]: ep for ep in endpoints}
        assert "hello" in eps_by_func
        assert eps_by_func["hello"]["methods"] == ["GET"]
        assert eps_by_func["hello"]["url"] == "/hello"
        assert "handle_item" in eps_by_func
        assert set(eps_by_func["handle_item"]["methods"]) == {"GET", "POST"}
        assert eps_by_func["handle_item"]["url"] == "/items/{id}"

    def test_extract_ninja_add_router(self, parser, isolated_temp_dir):
        """Detect django-ninja add_router() calls for sub-router registration."""
        path = _write_py_file(isolated_temp_dir, "urls.py", """
from ninja import NinjaAPI, Router
from .items_router import items_router

api = NinjaAPI()
api.add_router("/items/", items_router)

@api.get("/health")
def health(request):
    pass
""")
        result = parser.parse_file(path)
        # Should detect @api.get("/health") as endpoint
        endpoints = result["endpoints"]
        assert len(endpoints) == 1
        assert endpoints[0]["function"] == "health"
        assert endpoints[0]["url"] == "/health"
        # Should detect add_router in url_patterns
        patterns = result["url_patterns"]
        add_routers = [p for p in patterns if p.get("func") == "add_router"]
        assert len(add_routers) == 1
        assert add_routers[0]["url"] == "/items/"
        assert add_routers[0]["sub_router_var"] == "items_router"

    def test_extract_ninja_router_variable_detection(self, parser, isolated_temp_dir):
        """Detect Router() and NinjaAPI() instance creation."""
        path = _write_py_file(isolated_temp_dir, "inventory.py", """
from ninja import Router
from ninja import NinjaAPI

sales_router = Router()
inventory_api = NinjaAPI()
""")
        result = parser.parse_file(path)
        assert "django-ninja" in result["frameworks"]


    def test_express_js_routes(self, parser, isolated_temp_dir):
        """Detect Express.js route registrations (call expressions, not decorators)."""
        path = _write_js_file(isolated_temp_dir, "app.js", """
const express = require('express');
const app = express();
const router = express.Router();

router.get('/users', function(req, res) { res.json([]); });
router.post('/users', (req, res) => { res.json({}); });
app.use('/api', router);

// Non-route call with '/' starter — should NOT be caught by URL heuristic
const x = someObj.get('/not-a-route');
""")
        result = parser.parse_file(path)
        # app.use('/api', router) is a MOUNT; router.get/post are ENDPOINTS
        patterns = result["url_patterns"]
        mount_patterns = [p for p in patterns if p.get("func") == "add_router"]
        assert len(mount_patterns) >= 1
        api_use = next((p for p in mount_patterns if p["url"] == "/api"), None)
        assert api_use is not None
        assert api_use["sub_router_var"] == "router"
        # Express.js framework should be detected
        assert "express" in result["frameworks"]
        # Verb calls become real endpoints with methods
        eps = {(e["url"], tuple(e["methods"])): e for e in result["endpoints"]}
        assert ("/users", ("GET",)) in eps, result["endpoints"]
        assert ("/users", ("POST",)) in eps

    def test_fastapi_include_router(self, parser, isolated_temp_dir):
        """Detect FastAPI APIRouter.include_router() calls."""
        path = _write_py_file(isolated_temp_dir, "main.py", """
from fastapi import FastAPI, APIRouter

app = FastAPI()
users_router = APIRouter()
admin_router = APIRouter()

@users_router.get("/profile")
def get_profile(request):
    pass

app.include_router("/users", users_router)
app.include_router("/admin", admin_router)
""")
        result = parser.parse_file(path)
        # Should detect @users_router.get("/profile") as endpoint
        endpoints = result["endpoints"]
        assert len(endpoints) == 1
        assert endpoints[0]["function"] == "get_profile"
        assert endpoints[0]["url"] == "/profile"
        # Should detect include_router in url_patterns
        patterns = result["url_patterns"]
        mounts = [p for p in patterns if p.get("func") == "add_router"]
        assert len(mounts) == 2
        urls = {p["url"] for p in mounts}
        assert "/users" in urls
        assert "/admin" in urls

    def test_django_path_without_leading_slash(self, parser, isolated_temp_dir):
        """Django convention: path() without leading /."""
        path = _write_py_file(isolated_temp_dir, "urls.py", """
from django.urls import path
from . import views

urlpatterns = [
    path('catalog/products/', views.list_products),
    path('items/<int:id>/', views.item_detail),
]
""")
        result = parser.parse_file(path)
        patterns = result["url_patterns"]
        # Should still be detected by _extract_url_patterns (Django-specific)
        urls = [p["url"] for p in patterns]
        assert "catalog/products/" in urls
        assert "items/<int:id>/" in urls
        # Also should be in routes from _extract_routes (unified approach)
        # Bare path() calls without leading / should be accepted


    def test_template_strings_js(self, parser, isolated_temp_dir):
        """JS template literals as route paths."""
        path = _write_js_file(isolated_temp_dir, "routes.js", """
const router = require('express').Router();
router.get(`/users/${id}`, handler);
router.get('/static/path', handler);
""")
        result = parser.parse_file(path)
        urls = [e["url"] for e in result["endpoints"]]
        # Template string path should be detected as '/users/'
        assert any(u == '/users/' for u in urls), f"Expected '/users/' in {urls}"
        # Static path should also work
        assert '/static/path' in urls

    def test_fstring_route_path(self, parser, isolated_temp_dir):
        """Python f-string as route path in FastAPI."""
        path = _write_py_file(isolated_temp_dir, "fstring_routes.py", """
from fastapi import APIRouter
router = APIRouter()

BASE = "/api"
@router.get(f"{BASE}/items")
def list_items(): ...
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        assert len(endpoints) >= 1
        # Should extract at least the literal part after interpolation
        eps_with_url = [e for e in endpoints if e["url"]]
        assert any("/items" in e["url"] for e in eps_with_url)

    def test_fstring_route_path_prefix_first(self, parser, isolated_temp_dir):
        """Python f-string with literal prefix before interpolation."""
        path = _write_py_file(isolated_temp_dir, "fstring_routes2.py", """
from fastapi import APIRouter
router = APIRouter()

@router.get(f"/api/{id}")
def get_item(id: int): ...
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        eps_with_url = [e for e in endpoints if e["url"]]
        assert any("/api/" in e["url"] for e in eps_with_url)

    def test_router_prefix_apirouter(self, parser, isolated_temp_dir):
        """FastAPI APIRouter with prefix= at instantiation."""
        path = _write_py_file(isolated_temp_dir, "prefix_router.py", """
from fastapi import APIRouter
router = APIRouter(prefix="/api/v1/users")

@router.get("/{id}")
def get_user(id: int):
    return {"id": id}
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        assert len(endpoints) == 1
        ep = endpoints[0]
        assert ep["function"] == "get_user"
        assert ep["url"] == "/api/v1/users/{id}"

    def test_router_prefix_ninja_router(self, parser, isolated_temp_dir):
        """Django-ninja Router with a documentation prefix (or no prefix)."""
        path = _write_py_file(isolated_temp_dir, "ninja_prefix.py", """
from ninja import Router
items_router = Router()

@items_router.get("/list")
def list_items(request):
    pass
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        eps_by_func = {ep["function"]: ep for ep in endpoints}
        assert eps_by_func["list_items"]["url"] == "/list"
        # Now with prefix
        path2 = _write_py_file(isolated_temp_dir, "ninja_prefix2.py", """
from ninja import Router
items_router = Router(prefix="/items")

@items_router.get("/list")
def list_items(request):
    pass
""")
        result2 = parser.parse_file(path2)
        endpoints2 = result2["endpoints"]
        eps_by_func2 = {ep["function"]: ep for ep in endpoints2}
        assert eps_by_func2["list_items"]["url"] == "/items/list"

    def test_router_prefix_no_prefix(self, parser, isolated_temp_dir):
        """APIRouter without prefix should not change URL."""
        path = _write_py_file(isolated_temp_dir, "no_prefix.py", """
from fastapi import APIRouter
router = APIRouter()

@router.get("/items")
def list_items(): ...
""")
        result = parser.parse_file(path)
        assert result["endpoints"][0]["url"] == "/items"


    def test_express_chained_route(self, parser, isolated_temp_dir):
        """Express chained route: app.route('/path').get(handler).post(handler)."""
        path = _write_js_file(isolated_temp_dir, "chained.js", """
const express = require('express');
const app = express();

app.route('/items')
   .get((req, res) => { res.json([]); })
   .post((req, res) => { res.json({}); });

app.route('/users')
   .get(getUsers);
""")
        result = parser.parse_file(path)
        urls = [e["url"] for e in result["endpoints"]]
        assert '/items' in urls, f"Expected '/items' in {urls}"
        assert '/users' in urls, f"Expected '/users' in {urls}"

    def test_nextjs_file_based_routing(self, parser, isolated_temp_dir):
        """Next.js App Router: derive route from file path."""
        # Create a Next.js route.ts file inside app/api/users/[id]/
        dir_path = os.path.join(isolated_temp_dir, "my_app", "app", "api", "users", "[id]")
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, "route.ts")
        with open(path, 'w') as f:
            f.write("""
import { NextRequest, NextResponse } from 'next/server';

export async function GET(request: NextRequest) {
    return NextResponse.json({});
}

export async function POST(request: NextRequest) {
    return NextResponse.json({}, { status: 201 });
}
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        eps_by_method = {ep["function"]: ep for ep in endpoints}
        # Should detect GET /api/users/:id
        assert "GET" in eps_by_method
        assert eps_by_method["GET"]["url"] == "/api/users/:id"
        assert eps_by_method["GET"]["methods"] == ["GET"]
        # Should detect POST /api/users/:id
        assert "POST" in eps_by_method
        assert eps_by_method["POST"]["url"] == "/api/users/:id"
        # Should detect nextjs framework
        assert "nextjs" in result["frameworks"]

    def test_nextjs_page_routing(self, parser, isolated_temp_dir):
        """Next.js App Router: page.tsx gets default GET route."""
        dir_path = os.path.join(isolated_temp_dir, "my_app", "app", "dashboard")
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, "page.tsx")
        with open(path, 'w') as f:
            f.write("""
export default function Dashboard() {
    return <div>Dashboard</div>;
}
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        assert len(endpoints) >= 1
        assert endpoints[0]["url"] == "/dashboard"
        assert endpoints[0]["methods"] == ["GET"]


    def test_jsx_route_self_closing(self, parser, isolated_temp_dir):
        """React Router <Route path=\"...\" element={<Comp />} /> self-closing."""
        path = _write_js_file(isolated_temp_dir, "routes.jsx", """
import { Route } from 'react-router-dom';

export default function App() {
  return (
    <Route path="/users" element={<Users />} />
  );
}
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        jsx_routes = [e for e in endpoints if e.get("url") == "/users"]
        assert len(jsx_routes) >= 1
        assert jsx_routes[0]["function"] == "Users"


    def test_jsx_route_nested(self, parser, isolated_temp_dir):
        """React Router nested <Route> elements."""
        path = _write_js_file(isolated_temp_dir, "nested_routes.jsx", """
import { Route } from 'react-router-dom';

function App() {
  return (
    <Route path="/about">
      <Route path="team" element={<Team />} />
    </Route>
  );
}
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        urls = [e["url"] for e in endpoints]
        assert "/about" in urls


    def test_create_browser_router(self, parser, isolated_temp_dir):
        """React Router v6.4+ createBrowserRouter([{path, element}])."""
        path = _write_js_file(isolated_temp_dir, "router_config.jsx", """
import { createBrowserRouter } from 'react-router-dom';

const router = createBrowserRouter([
  { path: "/", element: <Root /> },
  { path: "/users", element: <Users /> },
]);
""")
        result = parser.parse_file(path)
        endpoints = result["endpoints"]
        urls = [e["url"] for e in endpoints]
        assert "/" in urls
        assert "/users" in urls

    def test_empty_string_path(self, parser, isolated_temp_dir):
        """Django path('', ...) should resolve to root '/'."""
        path = _write_py_file(isolated_temp_dir, "root_url.py", """
from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
]
""")
        result = parser.parse_file(path)
        patterns = result["url_patterns"]
        root_routes = [p for p in patterns if p["url"] == "/"]
        assert len(root_routes) >= 1

    def test_empty_string_include(self, parser, isolated_temp_dir):
        """Django path('', include(...)) for root includes."""
        path = _write_py_file(isolated_temp_dir, "root_include.py", """
from django.urls import path, include

urlpatterns = [
    path('', include('myapp.urls')),
]
""")
        result = parser.parse_file(path)
        patterns = result["url_patterns"]
        root_includes = [p for p in patterns if p["url"] == "/" and p.get("is_include")]
        assert len(root_includes) >= 1

    def test_no_urlpatterns(self, parser, isolated_temp_dir):
        path = _write_py_file(isolated_temp_dir, "nourls.py", """
def hello():
    return "world"
""")
        result = parser.parse_file(path)
        assert result["url_patterns"] == []

    def test_async_and_generator_functions(self, parser, isolated_temp_dir):
        py_path = _write_py_file(isolated_temp_dir, "async_gen.py", """
async def fetch_data():
    return await api_call()

def number_stream():
    for i in range(10):
        yield i

def sync_fn():
    return 42
""")
        res_py = parser.parse_file(py_path)
        funcs = {f["name"]: f for f in res_py["functions"]}
        assert funcs["fetch_data"]["is_async"] is True
        assert funcs["fetch_data"]["is_generator"] is False
        assert funcs["number_stream"]["is_async"] is False
        assert funcs["number_stream"]["is_generator"] is True
        assert funcs["sync_fn"]["is_async"] is False
        assert funcs["sync_fn"]["is_generator"] is False

        ts_path = os.path.join(isolated_temp_dir, "async_gen.ts")
        with open(ts_path, "w") as f:
            f.write("""
export async function loadSales() {
    return await fetch('/api/sales');
}

export function* genId() {
    yield 1;
}
""")
        res_ts = parser.parse_file(ts_path)
        ts_funcs = {f["name"]: f for f in res_ts["functions"]}
        assert ts_funcs["loadSales"]["is_async"] is True
        assert ts_funcs["genId"]["is_generator"] is True

    def test_param_count_extraction(self, parser, isolated_temp_dir):
        py_path = _write_py_file(isolated_temp_dir, "params.py", """
def no_params():
    pass

def single_param(a):
    pass

def multi_params(a, b, c=1, *args, **kwargs):
    pass

class Item:
    def method(self, x, y):
        pass
""")
        res_py = parser.parse_file(py_path)
        funcs = {f["name"]: f for f in res_py["functions"]}
        assert funcs["no_params"]["param_count"] == 0
        assert funcs["single_param"]["param_count"] == 1
        assert funcs["multi_params"]["param_count"] == 5

        cls = res_py["classes"][0]
        methods = {m["name"]: m for m in cls["methods"]}
        assert methods["method"]["param_count"] == 2

    def test_is_exported_extraction(self, parser, isolated_temp_dir):
        py_path = _write_py_file(isolated_temp_dir, "exports_test.py", """
def public_api():
    pass

def _internal_helper():
    pass

def __init_internal__():
    pass
""")
        res_py = parser.parse_file(py_path)
        funcs = {f["name"]: f for f in res_py["functions"]}
        assert funcs["public_api"]["is_exported"] is True
        assert funcs["_internal_helper"]["is_exported"] is False
        assert funcs["__init_internal__"]["is_exported"] is True

        ts_path = os.path.join(isolated_temp_dir, "exports_test.ts")
        with open(ts_path, "w") as f:
            f.write("""
export function exportedFunc() {}
function internalFunc() {}
""")
        res_ts = parser.parse_file(ts_path)
        ts_funcs = {f["name"]: f for f in res_ts["functions"]}
        assert ts_funcs["exportedFunc"]["is_exported"] is True
        assert ts_funcs["internalFunc"]["is_exported"] is False


    def test_ts_module_level_declarations(self, parser, isolated_temp_dir):
        """`export const X = pgTable(...)` must be indexed as a declaration (not
        a function), with its callee call and export status captured."""
        ts_path = os.path.join(isolated_temp_dir, "schema.ts")
        with open(ts_path, "w") as f:
            f.write("""
import { pgTable, serial, text } from 'drizzle-orm/pg-core';

export const usersTable = pgTable('users', {
    id: serial('id').primaryKey(),
    name: text('name'),
});

const client = db.connect('prod');
const env = process.env.NODE_ENV;

export const fetchUsers = () => fetch('/api/users/');
""")
        res = parser.parse_file(ts_path)
        decls = {d["name"]: d for d in res["declarations"]}

        # Call-assigned declarations are captured with their callee + export flag.
        assert "usersTable" in decls
        assert decls["usersTable"]["call"] == "pgTable"
        assert decls["usersTable"]["is_exported"] is True
        assert decls["usersTable"]["lines"]["start"] == 4

        assert "client" in decls
        assert decls["client"]["call"] == "db.connect"
        assert decls["client"]["is_exported"] is False

        # Plain (non-call) assignments are not declarations.
        assert "env" not in decls

        # Arrow-function consts are functions, NOT declarations.
        assert "fetchUsers" not in decls
        func_names = {f["name"] for f in res["functions"]}
        assert "fetchUsers" in func_names


    def test_jsx_element_calls_extraction(self, parser, isolated_temp_dir):
        jsx_path = os.path.join(isolated_temp_dir, "App.jsx")
        with open(jsx_path, "w") as f:
            f.write("""
import React from 'react';
import AchatPage from './AchatPage';
import { Header } from './Header';

export function App() {
    return (
        <div>
            <Header title="Dashboard" />
            <AchatPage />
        </div>
    );
}
""")
        res = parser.parse_file(jsx_path)
        funcs = {f["name"]: f for f in res["functions"]}
        assert "App" in funcs
        app_calls = funcs["App"]["calls"]
        assert "Header" in app_calls
        assert "AchatPage" in app_calls

    def test_anonymous_express_route_handlers_indexed(self, parser, isolated_temp_dir):
        """Anonymous `router.get('/x', async (req, res) => {...})` handlers must
        be extracted as traceable Function nodes, and the route's view_name must
        point at the synthetic handler name."""
        ts_path = os.path.join(isolated_temp_dir, "routes.ts")
        with open(ts_path, "w") as f:
            f.write("""
import { Router } from 'express';
import { db } from '@workspace/db';

const router = Router();

router.get('/rentals/:id/return', async (req, res) => {
  const id = Number(req.params.id);
  await db.update(rentalsTable).set({ status: 'returned' })
    .where(eq(rentalsTable.id, id)).returning();
  res.json(rental);
});

router.use('/admin', adminAuth);

export default router;
""")
        res = parser.parse_file(ts_path)
        handlers = {f["name"]: f for f in res["functions"] if f["name"].startswith("handler_")}
        assert len(handlers) == 1, handlers
        handler_name, handler = next(iter(handlers.items()))
        assert handler_name.startswith("handler_get_L"), handler_name
        assert handler["is_async"] is True
        assert "db.update" in handler["calls"]
        assert "res.json" in handler["calls"]

        routes = {e["url"]: e for e in res["endpoints"]}
        assert routes["/rentals/:id/return"]["function"] == handler_name

        # Named mount handlers (middleware identifiers) stay as-is, not synthesized.
        mount_routes = {u["url"]: u for u in res["url_patterns"]}
        assert mount_routes["/admin"]["view_name"] == "adminAuth"

    def test_http_calls_resolve_generated_client_url_builders(self, parser, isolated_temp_dir):
        """Generated API clients call wrappers with an indirect URL builder
        (customFetch<Rental>(getReturnRentalUrl(id), ...)). These must resolve to
        the builder's path with a proper HTTP method."""
        ts_path = os.path.join(isolated_temp_dir, "api.ts")
        with open(ts_path, "w") as f:
            f.write("""
export const getReturnRentalUrl = (id: number) => {
  return `/api/rentals/${id}/return`
}

export const returnRental = async (id: number, input: object) => {
  return customFetch<Rental>(getReturnRentalUrl(id), { method: 'POST', body: JSON.stringify(input) });
}
""")
        res = parser.parse_file(ts_path)
        calls = res.get("http_calls", [])
        matched = [c for c in calls if c["url"] == "/api/rentals/{id}/return"]
        assert matched, calls
        assert matched[0]["method"] == "POST"
        assert matched[0]["lib"] == "customFetch"
