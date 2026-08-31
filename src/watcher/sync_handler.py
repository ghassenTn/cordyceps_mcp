"""
File System Watcher — Sync Handler for EngramDB.
Watches for file changes and incrementally updates the CSR graph.
Uses the high-performance Rust-native EngramDB engine via EngramClient.
"""
import ast
import os
import logging
import queue
import threading
import functools
from watchdog.events import FileSystemEventHandler
# pyrefly: ignore [missing-import]
from src.database.graph_client import INDEX_META_FILENAME, SNAPSHOT_FILENAME

logger = logging.getLogger(__name__)

# Shared event queue for thread-safe dispatch
_sync_queue = queue.Queue()

def _python_import_bindings(source: str, file_path: str) -> dict:
    """Return Python import bindings grouped by qualified lexical scope.

    The existing ``imports`` field stays unchanged for query compatibility. This
    compact map is persisted on the File node so Rust can distinguish, for
    example, ``from a import work`` from ``from b import work`` when duplicate
    symbol names exist.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError):
        return {}

    normalized_path = file_path.replace("\\", "/")
    package_parts = normalized_path.split("/")[:-1]
    bindings: dict[str, dict[str, dict | list[dict]]] = {}

    def resolve_module(module: str | None, level: int) -> str:
        if level:
            keep = max(0, len(package_parts) - (level - 1))
            parts = package_parts[:keep]
            if module:
                parts.extend(module.split("."))
            return "/".join(part for part in parts if part)
        return (module or "").replace(".", "/")

    class ImportVisitor(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []

        @property
        def scope_name(self) -> str:
            return ".".join(self.scope)

        def _record(self, local: str, *, module: str, symbol=None,
                    kind: str, qualifier: str | None = None):
            if not local or local == "*":
                return
            entry = {
                "module": module,
                "symbol": symbol,
                "kind": kind,
            }
            if qualifier:
                entry["qualifier"] = qualifier
            scope = bindings.setdefault(self.scope_name, {})
            current = scope.get(local)
            if current is None:
                scope[local] = entry
            elif isinstance(current, list):
                if entry not in current:
                    current.append(entry)
            elif current != entry:
                scope[local] = [current, entry]

        def visit_Import(self, node):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                self._record(
                    local,
                    module=resolve_module(alias.name, 0),
                    kind="module",
                    qualifier=(alias.name if alias.asname is None and "." in alias.name
                               else None),
                )

        def visit_ImportFrom(self, node):
            module = resolve_module(node.module, node.level)
            for alias in node.names:
                self._record(
                    alias.asname or alias.name,
                    module=module,
                    symbol=alias.name,
                    kind="from",
                )

        def _visit_scope(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = _visit_scope
        visit_AsyncFunctionDef = _visit_scope
        visit_ClassDef = _visit_scope

    ImportVisitor().visit(tree)
    return bindings


def _python_shadowed_names(source: str) -> dict:
    """Collect lexical bindings that must stop import/global fallback lookup."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError):
        return {}

    shadows: dict[str, set[str]] = {}

    def bound_names(node) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            result = set()
            for item in node.elts:
                result.update(bound_names(item))
            return result
        if isinstance(node, ast.Starred):
            return bound_names(node.value)
        return set()

    class ShadowVisitor(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []
            self.nonlocal_bindings: dict[str, set[str]] = {}

        @property
        def scope_name(self) -> str:
            return ".".join(self.scope)

        def _add(self, names):
            excluded = self.nonlocal_bindings.get(self.scope_name, set())
            shadows.setdefault(self.scope_name, set()).update(set(names) - excluded)

        def visit_Import(self, node):
            return

        def visit_ImportFrom(self, node):
            return

        def visit_Global(self, node):
            self.nonlocal_bindings.setdefault(self.scope_name, set()).update(node.names)
            shadows.setdefault(self.scope_name, set()).difference_update(node.names)

        def visit_Nonlocal(self, node):
            self.nonlocal_bindings.setdefault(self.scope_name, set()).update(node.names)
            shadows.setdefault(self.scope_name, set()).difference_update(node.names)

        def visit_Assign(self, node):
            for target in node.targets:
                self._add(bound_names(target))
            self.visit(node.value)

        def visit_AnnAssign(self, node):
            self._add(bound_names(node.target))
            if node.value is not None:
                self.visit(node.value)

        def visit_AugAssign(self, node):
            self._add(bound_names(node.target))
            self.visit(node.value)

        def visit_NamedExpr(self, node):
            self._add(bound_names(node.target))
            self.visit(node.value)

        def visit_For(self, node):
            self._add(bound_names(node.target))
            self.generic_visit(node)

        visit_AsyncFor = visit_For

        def visit_With(self, node):
            for item in node.items:
                if item.optional_vars is not None:
                    self._add(bound_names(item.optional_vars))
            self.generic_visit(node)

        visit_AsyncWith = visit_With

        def visit_ExceptHandler(self, node):
            if node.name:
                self._add({node.name})
            self.generic_visit(node)

        def _visit_function(self, node):
            self.scope.append(node.name)
            all_args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg:
                all_args.append(node.args.vararg)
            if node.args.kwarg:
                all_args.append(node.args.kwarg)
            self._add({arg.arg for arg in all_args})
            for statement in node.body:
                self.visit(statement)
            self.scope.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            for statement in node.body:
                self.visit(statement)
            self.scope.pop()

    ShadowVisitor().visit(tree)
    return {scope: sorted(names) for scope, names in shadows.items() if names}


def _python_receiver_types(source: str) -> dict:
    """Infer receiver classes from annotations and direct constructor assigns."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError):
        return {}

    receiver_types: dict[str, dict[str, str | None]] = {}

    def dotted_name(node) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = dotted_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return dotted_name(ast.parse(node.value, mode="eval").body)
            except (SyntaxError, ValueError):
                return node.value.strip("'\"")
        if isinstance(node, ast.Subscript):
            base = dotted_name(node.value)
            wrapper = base.rsplit(".", 1)[-1] if base else ""
            if wrapper == "Annotated" and isinstance(node.slice, ast.Tuple):
                return dotted_name(node.slice.elts[0]) if node.slice.elts else None
            if wrapper in {"Optional", "ClassVar", "Final"}:
                return dotted_name(node.slice)
            if wrapper == "Union":
                items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                names = {dotted_name(item) for item in items} - {None, "None"}
                return next(iter(names)) if len(names) == 1 else None
            return base
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            def union_names(item):
                if isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitOr):
                    return union_names(item.left) | union_names(item.right)
                name = dotted_name(item)
                return {name} if name not in (None, "None") else set()

            names = union_names(node)
            return next(iter(names)) if len(names) == 1 else None
        if isinstance(node, ast.Tuple):
            names = {dotted_name(item) for item in node.elts} - {None, "None"}
            if len(names) == 1:
                return next(iter(names))
        return None

    def target_name(node) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = target_name(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    class ReceiverVisitor(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []
            self.scope_kinds: list[str] = []

        @property
        def scope_name(self) -> str:
            return ".".join(self.scope)

        def _set(self, receiver: str, type_name: str | None, scope: str | None = None):
            if not receiver:
                return
            key = self.scope_name if scope is None else scope
            bucket = receiver_types.setdefault(key, {})
            if receiver not in bucket:
                bucket[receiver] = type_name
            elif bucket[receiver] != type_name:
                bucket[receiver] = None

        def _known_type(self, value) -> str | None:
            if isinstance(value, ast.Call):
                candidate = dotted_name(value.func)
                if candidate and candidate.rsplit(".", 1)[-1][:1].isupper():
                    return candidate
            if isinstance(value, ast.Name):
                return receiver_types.get(self.scope_name, {}).get(value.id)
            return None

        def _record_target(self, target, type_name: str | None):
            receiver = target_name(target)
            if receiver is None:
                return
            self._set(receiver, type_name)
            if receiver.startswith("self."):
                for index in range(len(self.scope_kinds) - 1, -1, -1):
                    if self.scope_kinds[index] == "class":
                        self._set(receiver, type_name, ".".join(self.scope[:index + 1]))
                        break
            elif self.scope_kinds and self.scope_kinds[-1] == "class":
                self._set(f"self.{receiver}", type_name)

        def visit_Import(self, node):
            return

        def visit_ImportFrom(self, node):
            return

        def visit_AnnAssign(self, node):
            self._record_target(node.target, dotted_name(node.annotation))
            if node.value is not None:
                self.visit(node.value)

        def visit_Assign(self, node):
            inferred = self._known_type(node.value)
            for target in node.targets:
                self._record_target(target, inferred)
            self.visit(node.value)

        def _visit_function(self, node):
            self.scope.append(node.name)
            self.scope_kinds.append("function")
            all_args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg:
                all_args.append(node.args.vararg)
            if node.args.kwarg:
                all_args.append(node.args.kwarg)
            for arg in all_args:
                if arg.annotation is not None:
                    self._set(arg.arg, dotted_name(arg.annotation))
            for statement in node.body:
                self.visit(statement)
            self.scope_kinds.pop()
            self.scope.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.scope_kinds.append("class")
            for statement in node.body:
                self.visit(statement)
            self.scope_kinds.pop()
            self.scope.pop()

    ReceiverVisitor().visit(tree)
    return receiver_types


# Thread-safe debounce tracking
_debounce_lock = threading.Lock()
_debounce_timers = {}


def get_sync_queue():
    return _sync_queue


class GraphSyncHandler(FileSystemEventHandler):
    """
    Watchdog handler that queues file change events.
    The main thread polls the queue to process them safely.
    """
    EXCLUDED_DIRS = {
        'node_modules', 'venv', 'env', '.venv', '.env',
        '__pycache__', 'target', 'dist', 'build', 'out',
        'dist-electron', 'electron', 'assets',
        'migrations', 'alembic', '.git', '.idea', '.vscode',
        'coverage', 'htmlcov', '.pytest_cache', '.hypothesis',
        '.next', '.nuxt', 'storybook-static', '.nyc_output', 'esm', 'cjs',
        # Known test fixture directories (NOT actual test dirs — those are indexed)
        'route_detection_tests', 'fixtures', 'test_fixtures', 'test_data',
    }
    # Ambiguous names that are ALSO legitimate source directories in some
    # projects (e.g. packages/*/src/env in TS monorepos). Only excluded when
    # they actually look like a virtualenv.
    VENV_CANDIDATE_NAMES = {'venv', 'env'}
    INTERNAL_ARTIFACT_NAMES = {INDEX_META_FILENAME, SNAPSHOT_FILENAME}

    @staticmethod
    def is_internal_artifact(path: str) -> bool:
        """Our own persistence sidecars — never source files, never watched."""
        return os.path.basename(path) in GraphSyncHandler.INTERNAL_ARTIFACT_NAMES

    @staticmethod
    def is_excluded_dir(dir_name: str, abs_path: str, excluded) -> bool:
        if dir_name not in excluded:
            return False
        if dir_name in GraphSyncHandler.VENV_CANDIDATE_NAMES:
            return (os.path.exists(os.path.join(abs_path, "pyvenv.cfg"))
                    or os.path.exists(os.path.join(abs_path, "bin", "python"))
                    or os.path.exists(os.path.join(abs_path, "bin", "python3"))
                    or os.path.exists(os.path.join(abs_path, "Scripts", "python.exe")))
        return True

    def __init__(self, workspace_path):
        if hasattr(workspace_path, "workspace_path"):
            self.workspace_path = str(workspace_path.workspace_path)
        elif hasattr(workspace_path, "client") and hasattr(workspace_path.client, "_client"):
            self.workspace_path = str(workspace_path.client._client.workspace_path)
        else:
            self.workspace_path = str(workspace_path)
        from src.database.parser.language_adapter import SUPPORTED_EXTENSIONS
        self.supported_extensions = SUPPORTED_EXTENSIONS
        self._parser = None
        # Merge exclusions from env var (comma-separated)
        extra = os.environ.get("CORDYCEPS_EXCLUDE", "")
        if extra:
            self._excluded = self.EXCLUDED_DIRS | {d.strip() for d in extra.split(",") if d.strip()}
        else:
            self._excluded = self.EXCLUDED_DIRS

    @functools.lru_cache(maxsize=512)
    def _has_source_files(self, dir_path: str) -> bool:
        """Check if directory recursively contains any supported source files (respecting exclusions)."""
        if not os.path.isdir(dir_path):
            return False
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if not d.startswith('.')
                       and not self.is_excluded_dir(d, os.path.join(root, d), self._excluded)]
            for f in files:
                if f.endswith(self.supported_extensions):
                    return True
        return False

    @property
    def parser(self):
        if self._parser is None:
            from src.database.parser.ast_parser import UniversalCodeParser
            self._parser = UniversalCodeParser()
        return self._parser

    def _relative_path(self, absolute_path: str) -> str:
        try:
            return os.path.relpath(absolute_path, self.workspace_path)
        except ValueError:
            return absolute_path

    def update_file_in_graph(self, file_path: str, skip_rebuild: bool = False, pre_parsed_data: dict = None):
        """
        Full update: remove old data, parse file, inject new nodes/edges.
        Called from the MAIN thread.
        
        Args:
            file_path: Absolute path to the file.
            skip_rebuild: If True, skip calling rebuild(). Useful for batch scans.
        """
        from src.database import get_graph_db
        db = get_graph_db(self.workspace_path)

        # 1. Invalidate old data for this file (O(k) in Rust)
        rel_path = self._relative_path(file_path)
        removed = db.client.invalidate_file(rel_path)
        if removed > 0:
            logger.debug(f"Invalidated {removed} old nodes from {rel_path}")

        # Invalidate python-files cache since file system changed
        self._has_source_files.cache_clear()

        if not os.path.exists(file_path):
            if not skip_rebuild:
                db.client.rebuild()
            return

        try:
            if pre_parsed_data is not None:
                parsed_data = pre_parsed_data
            else:
                parsed_data = self.parser.parse_file(file_path)
        except Exception as e:
            logger.error(f"Failed to parse '{rel_path}': {e}")
            return

        # 1.5 Register Folder nodes (only if directory actually has .py files)
        folder_path = os.path.dirname(rel_path)
        if folder_path:
            parts = folder_path.split("/")
            current_path = ""
            for part in parts:
                if not part: continue
                parent = current_path
                current_path = f"{current_path}/{part}" if current_path else part
                abs_dir = os.path.join(self.workspace_path, current_path)

                if not self._has_source_files(abs_dir):
                    continue

                if not db.client.contains(current_path):
                    db.client.add_node(current_path, "Folder", part, current_path)
                if parent:
                    db.client.add_structural_edge(current_path, parent)

        # 2. Register the File node
        extra_file_meta = {}
        file_body = parsed_data.get('file_body', '')
        if file_body:
            extra_file_meta['body'] = file_body
        if rel_path.endswith('.py') and file_body:
            extra_file_meta['python_import_bindings'] = _python_import_bindings(
                file_body, rel_path)
            extra_file_meta['python_receiver_types'] = _python_receiver_types(file_body)
            extra_file_meta['python_shadowed_names'] = _python_shadowed_names(file_body)
        if 'imports' in parsed_data:
            extra_file_meta['imports'] = parsed_data['imports']
        import_lines = parsed_data.get('import_lines') or {}
        if import_lines:
            extra_file_meta['import_lines'] = import_lines
        url_patterns = parsed_data.get('url_patterns', [])
        if url_patterns:
            extra_file_meta['url_patterns'] = url_patterns
        frameworks = parsed_data.get('frameworks', [])
        if frameworks:
            extra_file_meta['frameworks'] = frameworks
        file_http_calls = parsed_data.get('http_calls', [])
        if file_http_calls:
            extra_file_meta['http_calls'] = file_http_calls
        total_lines = file_body.count('\n') + 1 if file_body else 0
        db.client.add_node(rel_path, "File", os.path.basename(rel_path), rel_path,
                           lines={"start": 1, "end": total_lines}, _extra=extra_file_meta)
        if folder_path:
            # Folder CONTAINS File edge
            db.client.add_structural_edge(rel_path, folder_path)

        # 2.5 Register Route nodes (from urls.py url_patterns and add_router calls)
        url_patterns = parsed_data.get('url_patterns', [])
        for up in url_patterns:
            if up.get('is_include') and up.get('func') == 'path':
                continue  # include() routes resolved lazily
            url = up['url']
            view_name = up.get('view_name', '')
            route_name = up.get('name', '')
            # Route node_id: {rel_path}:{url}
            route_id = f"{rel_path}:{url}"
            extra_route = {
                'view_name': view_name,
                'route_name': route_name,
                'url': url,
                'func': up.get('func', 'path'),
                'methods': up.get('methods', []),
            }
            db.client.add_node(route_id, "Route", url, rel_path, _extra=extra_route)
            # File DEFINES_ROUTE edge
            db.client.add_structural_edge(route_id, rel_path)

            # Route→View edges are created in resolve_url_patterns() post-processing pass

        # 3. Register Classes and Methods
        for cls in parsed_data.get('classes', []):
            # Nested classes carry a dotted node_name (e.g. Outer.Inner); top-level
            # ones fall back to their bare name.
            cls_key = cls.get('node_name') or cls['name']
            cls_id = f"{rel_path}:{cls_key}"
            django_relations = cls.get('django_relations', [])
            extra_meta = {}
            if 'body' in cls:
                extra_meta['body'] = cls['body']
            if django_relations:
                extra_meta['django_relations'] = django_relations
            if cls.get('decorators'):
                extra_meta['decorators'] = cls['decorators']
            if cls.get('base_classes'):
                extra_meta['base_classes'] = cls['base_classes']
                extra_meta['inherits'] = cls['base_classes']
            # Aggregate template_refs and url_patterns from class methods
            all_template_refs = []
            for method in cls.get('methods', []):
                all_template_refs.extend(method.get('template_refs', []))
            if all_template_refs:
                extra_meta['template_refs'] = all_template_refs
            db.client.add_node(cls_id, "Class", cls['name'], rel_path,
                               signature=cls.get('signature'), docstring=cls.get('docstring'), 
                               lines=cls.get('lines'), 
                               django_relations=cls.get('django_relations'),
                               is_exported=cls.get('is_exported'),
                               _extra=extra_meta)
            # File DEFINES_CLASS edge
            db.client.add_structural_edge(cls_id, rel_path)
            # Nested class → enclosing class edge (Outer.Inner → Outer)
            if '.' in cls_key:
                parent_id = f"{rel_path}:{cls_key.rsplit('.', 1)[0]}"
                if db.client.contains(parent_id):
                    db.client.add_structural_edge(cls_id, parent_id)
            
            # Resolve Django relations
            if cls.get('django_relations'):
                db.client.resolve_and_connect_django(cls_id, cls['django_relations'])

            for method in cls.get('methods', []):
                method_id = f"{cls_id}.{method['name']}"
                extra_meta = {}
                if 'body' in method:
                    extra_meta['body'] = method['body']
                if method.get('decorators'):
                    extra_meta['decorators'] = method['decorators']
                template_refs = method.get('template_refs', [])
                if template_refs:
                    extra_meta['template_refs'] = template_refs
                api_endpoint = method.get('api_endpoint')
                if api_endpoint:
                    extra_meta['api_endpoint'] = api_endpoint
                m_lines = method.get('lines', {})
                m_start = m_lines.get('start', 0) if isinstance(m_lines, dict) else 0
                m_end = m_lines.get('end', 0) if isinstance(m_lines, dict) else 0
                if m_start and m_end and file_http_calls:
                    m_http = [c for c in file_http_calls if m_start <= c.get('line', 0) <= m_end]
                    if m_http:
                        extra_meta['http_calls'] = m_http
                db.client.add_node(method_id, "Function", method['name'], rel_path,
                                   signature=method.get('signature'), 
                                   docstring=method.get('docstring'), 
                                   lines=method.get('lines'),
                                   returns=method.get('returns'),
                                   calls=method.get('calls'),
                                   is_async=method.get('is_async'),
                                   is_generator=method.get('is_generator'),
                                   param_count=method.get('param_count'),
                                   is_exported=method.get('is_exported'),
                                   _extra=extra_meta)
                # Class HAS_METHOD edge
                db.client.add_structural_edge(method_id, cls_id)
                
        # 4. Register Standalone Functions
        for func in parsed_data.get('functions', []):
            # Nested functions carry a dotted node_name (e.g. outer.inner or
            # Class.method.helper); top-level ones fall back to their bare name.
            func_key = func.get('node_name') or func['name']
            func_id = f"{rel_path}:{func_key}"
            extra_meta = {}
            if 'body' in func:
                extra_meta['body'] = func['body']
            if func.get('decorators'):
                extra_meta['decorators'] = func['decorators']
            template_refs = func.get('template_refs', [])
            if template_refs:
                extra_meta['template_refs'] = template_refs
            api_endpoint = func.get('api_endpoint')
            if api_endpoint:
                extra_meta['api_endpoint'] = api_endpoint
            f_lines = func.get('lines', {})
            f_start = f_lines.get('start', 0) if isinstance(f_lines, dict) else 0
            f_end = f_lines.get('end', 0) if isinstance(f_lines, dict) else 0
            if f_start and f_end and file_http_calls:
                f_http = [c for c in file_http_calls if f_start <= c.get('line', 0) <= f_end]
                if f_http:
                    extra_meta['http_calls'] = f_http
            db.client.add_node(func_id, "Function", func['name'], rel_path,
                               signature=func.get('signature'), 
                               docstring=func.get('docstring'), 
                               lines=func.get('lines'),
                               returns=func.get('returns'),
                               calls=func.get('calls'),
                               is_async=func.get('is_async'),
                               is_generator=func.get('is_generator'),
                               param_count=func.get('param_count'),
                               is_exported=func.get('is_exported'),
                               _extra=extra_meta)
            # File DEFINES_FUNC edge
            db.client.add_structural_edge(func_id, rel_path)
            # Nested function → enclosing def edge (outer.inner → outer,
            # Class.method.helper → Class.method)
            if '.' in func_key:
                parent_id = f"{rel_path}:{func_key.rsplit('.', 1)[0]}"
                if db.client.contains(parent_id):
                    db.client.add_structural_edge(func_id, parent_id)

        # 4.4 Register module-level declarations (const X = pgTable(...), const api = fetch(...))
        for decl in parsed_data.get('declarations', []):
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

        # 4.5 Register Route nodes from decorator endpoints (@router.get, @app.post, etc.)
        # Grouped by URL so same-path different-method routes (GET+POST on '/') share
        # one Route node instead of overwriting each other.
        endpoints_grouped = {}
        endpoints_order = []
        for ep in parsed_data.get('endpoints', []):
            url = ep.get('url', '')
            if not url:
                continue
            if url not in endpoints_grouped:
                endpoints_grouped[url] = []
                endpoints_order.append(url)
            endpoints_grouped[url].append(ep)
        for url in endpoints_order:
            group = endpoints_grouped[url]
            methods = []
            funcs = []
            framework = ''
            for ep in group:
                for m in ep.get('methods', []):
                    if m and m not in methods:
                        methods.append(m)
                fn_name = ep.get('function', '')
                if fn_name and fn_name not in funcs:
                    funcs.append(fn_name)
                if ep.get('framework'):
                    framework = ep['framework']
            route_id = f"{rel_path}:{url}"
            extra_route = {
                'view_name': funcs[0] if funcs else '',
                'view_names': funcs,
                'url': url,
                'methods': methods,
                'framework': framework,
                'func': 'decorator',
                'source_var': group[0].get('source_var', ''),
            }
            db.client.add_node(route_id, "Route", url, rel_path, _extra=extra_route)
            db.client.add_structural_edge(route_id, rel_path)
            # Connect Route to its handler function(s) if they exist
            for fn_name in funcs:
                func_id = f"{rel_path}:{fn_name}"
                if db.client.contains(func_id):
                    db.client.add_generated_edge(route_id, func_id)

        # 4.6 Register Middleware nodes (app.use, MIDDLEWARE=[...], @app.before_request, etc.)
        for mw in parsed_data.get('middleware', []):
            mw_name = mw.get('name', '')
            if not mw_name:
                continue
            mw_id = f"{rel_path}:mw:{mw_name}"
            extra_mw = {
                'handler_var': mw.get('handler_var', ''),
                'source_var': mw.get('source_var', ''),
                'url': mw.get('url', ''),
                'middleware_type': mw.get('middleware_type', ''),
                'line': mw.get('line', 0),
            }
            db.client.add_node(mw_id, "Middleware", mw_name, rel_path, _extra=extra_mw)
            db.client.add_structural_edge(mw_id, rel_path)

        # 4.7 Store exports metadata on the file node
        exports_data = parsed_data.get('exports', [])
        if exports_data:
            db.client.add_to_extra_meta(rel_path, "exports", exports_data)

        # 5. Connect IMPORTS
        for imp in parsed_data.get('imports', []):
            imp_path = self._resolve_import_path(imp, rel_path)
            if imp_path is None:
                continue
            # Ensure the imported file node exists in metadata
            if not db.client.contains(imp_path):
                db.client.add_node(imp_path, "File", os.path.basename(imp_path), imp_path)
            # Import edge: target file affects importer
            db.client.add_structural_edge(imp_path, rel_path)

        # 6. Connect CALLS (O(1) per call via Rust name index)
        # We do this AFTER all nodes for the current file are registered
        for cls in parsed_data.get('classes', []):
            cls_key = cls.get('node_name') or cls['name']
            for method in cls.get('methods', []):
                caller_id = f"{rel_path}:{cls_key}.{method['name']}"
                calls = method.get('calls', [])
                if calls:
                    db.client.resolve_and_connect_calls(caller_id, calls)

        for func in parsed_data.get('functions', []):
            func_key = func.get('node_name') or func['name']
            func_id = f"{rel_path}:{func_key}"
            calls = func.get('calls', [])
            if calls:
                db.client.resolve_and_connect_calls(func_id, calls)

        # 7. Resolve Django ORM relationships (O(1) via name index)
        django_classes = [c for c in parsed_data.get('classes', []) if c.get('django_relations')]
        if django_classes:
            all_meta = db.client.get_all_metadata()
            class_index = {}
            for nid, meta in all_meta.items():
                if meta.get('type') == 'Class':
                    class_index[meta.get('name')] = nid
            for cls in django_classes:
                cls_key = cls.get('node_name') or cls['name']
                cls_id = f"{rel_path}:{cls_key}"
                for rel in cls['django_relations']:
                    target_name = rel.get('related_model', '')
                    if not target_name:
                        continue
                    target_id = class_index.get(target_name)
                    if target_id and target_id != cls_id:
                        db.client.add_structural_edge(target_id, cls_id)

        # 8. Rebuild CSR (Batching optimization: skip if in initial scan)
        if not skip_rebuild:
            db.client.rebuild()
            logger.info(f"Updated '{rel_path}' in EngramDB graph.")

    def remove_file_from_graph(self, file_path: str, skip_rebuild: bool = False):
        """Remove all nodes belonging to a file."""
        self._has_source_files.cache_clear()
        from src.database import get_graph_db
        db = get_graph_db(self.workspace_path)
        rel_path = self._relative_path(file_path)
        removed = db.client.invalidate_file(rel_path)
        if not skip_rebuild:
            db.client.rebuild()
            logger.info(f"Removed '{rel_path}' from graph ({removed} nodes).")

    # --- Watchdog event handlers: push to queue, don't call Rust directly ---

    def _resolve_import_path(self, imp: str, source_rel_path: str) -> str:
        _, source_ext = os.path.splitext(source_rel_path)
        if source_ext == '.py':
            source_dir = os.path.dirname(source_rel_path)
            if imp.startswith('.'):
                dots = 0
                for char in imp:
                    if char == '.':
                        dots += 1
                    else:
                        break
                rest = imp[dots:].replace('.', '/')
                curr_dir = source_dir
                for _ in range(dots - 1):
                    curr_dir = os.path.dirname(curr_dir)
                rel_base = os.path.join(curr_dir, rest) if rest else curr_dir
            else:
                rel_base = imp.replace('.', '/')

            py_file = rel_base + ".py"
            if os.path.exists(os.path.join(self.workspace_path, py_file)):
                return os.path.normpath(py_file)

            init_file = os.path.join(rel_base, "__init__.py")
            if os.path.exists(os.path.join(self.workspace_path, init_file)):
                return os.path.normpath(init_file)

            return None

        if not imp.startswith('.'):
            return None

        source_dir = os.path.dirname(source_rel_path)
        rel_base = imp[2:] if imp.startswith('./') else imp
        abs_source_dir = os.path.join(self.workspace_path, source_dir)
        candidate = os.path.join(abs_source_dir, rel_base)

        for try_ext in ['.js', '.jsx', '.ts', '.tsx']:
            full = candidate + try_ext
            if os.path.exists(full):
                return os.path.relpath(full, self.workspace_path)
            index_candidate = os.path.join(candidate, f"index{try_ext}")
            if os.path.exists(index_candidate):
                return os.path.relpath(index_candidate, self.workspace_path)

        return None

    def _is_excluded(self, path: str) -> bool:
        """Check if path contains any excluded directories."""
        if path.endswith('.d.ts'):
            return True
        parts = path.replace('\\', '/').split('/')
        for part in parts:
            if part.startswith('.') and part not in ('.', '..'):
                return True
            if part in self._excluded:
                return True
        return False

    def on_modified(self, event):
        """Queue file modification event with debouncing."""
        if event.is_directory or not event.src_path.endswith(self.supported_extensions):
            return
        if self._is_excluded(event.src_path):
            return
        _enqueue_debounced_event('update', event.src_path)

    def on_created(self, event):
        """Queue file creation event with debouncing."""
        if not event.is_directory and event.src_path.endswith(self.supported_extensions):
            if self._is_excluded(event.src_path):
                return
            _enqueue_debounced_event('update', event.src_path)

    def on_deleted(self, event):
        """Queue file deletion event with debouncing."""
        if not event.is_directory and event.src_path.endswith(self.supported_extensions):
            if self._is_excluded(event.src_path):
                return
            _enqueue_debounced_event('delete', event.src_path)

    def on_moved(self, event):
        """Queue file move event with debouncing."""
        if not event.is_directory:
            if event.src_path.endswith(self.supported_extensions) and not self._is_excluded(event.src_path):
                _enqueue_debounced_event('delete', event.src_path)
            if event.dest_path.endswith(self.supported_extensions) and not self._is_excluded(event.dest_path):
                _enqueue_debounced_event('update', event.dest_path)

def _enqueue_debounced_event(action: str, file_path: str, debounce_threshold: float = 0.5):
    """
    Thread-safe debounce wrapper for file system events.
    Waits for debounce_threshold seconds after the LAST event before queuing.
    """
    with _debounce_lock:
        # Cancel existing timer if one is already running for this file
        if file_path in _debounce_timers:
            _debounce_timers[file_path].cancel()
            
        def push_to_queue():
            _sync_queue.put((action, file_path))
            with _debounce_lock:
                _debounce_timers.pop(file_path, None)
                
        # Start a new timer
        timer = threading.Timer(debounce_threshold, push_to_queue)
        _debounce_timers[file_path] = timer
        timer.start()
