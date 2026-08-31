"""
EngramDB Client.
Uses the high-performance Rust-native PyMetadataEngine for both
metadata indexing and CSR graph operations.
"""
import os
import logging
import re

# ESM import forms: import def, { a as b } from 'mod'; import * as ns from 'mod'
_ESM_IMPORT_RE = re.compile(
    r"^import\s+"
    r"(?:\*(?:\s+as\s+(?P<ns>\w+))?"
    r"|\{(?P<named>[^}]*)\}"
    r"|(?P<default>\w+)(?:\s*,\s*\{(?P<named2>[^}]*)\})?"
    r")"
    r"\s+from\s+[\"'](?P<mod>[^\"']+)[\"']"
)

logger = logging.getLogger(__name__)

try:
    import engramdb
except ImportError:
    logger.error("EngramDB Rust extension not found. Please run 'maturin develop' in engramdb directory.")
    raise


def _dedupe_preserve_order(items: list) -> list:
    """Deduplicate a list while preserving first-occurrence order."""
    if not items:
        return items
    return list(dict.fromkeys(items))


# Sidecar file next to the Rust binary snapshot (.engram_snapshot.bin) that
# records which indexing configuration produced the current graph. Used to
# detect a stale index (config changed without a rescan) so queries are never
# silently answered from outdated data.
INDEX_META_FILENAME = ".cordyceps_index_meta.json"
# Written by the Rust engine on build(); restored automatically on init.
SNAPSHOT_FILENAME = ".engram_snapshot.bin"


class EngramClient:
    """
    Client for EngramDB.
    Wraps the unified Rust engine which handles:
    - O(1) Metadata indexing
    - O(1) Call resolution
    - Ultra-fast CSR graph operations
    - Binary persistence
    """

    def __init__(self, workspace_path=None):
        self.workspace_path = workspace_path or os.environ.get("WORKSPACE_PATH", os.getcwd())
        # The Rust constructor handles restoring from binary snapshot automatically
        self.engine = engramdb.PyMetadataEngine(self.workspace_path)
        self.is_read_only = False
        # Extra metadata not supported by the Rust engine (e.g. Django ORM relations)
        self._extra_meta = {}
        self._hydrate_extra_meta()

        logger.info(f"EngramDB Rust engine initialized for: {self.workspace_path}")
        self.clean_stale_files()

    def _hydrate_extra_meta(self) -> None:
        """Restore Python post-processing metadata from Rust snapshots."""
        import json
        for node_id, meta in self.engine.get_all_metadata().items():
            raw = meta.get("extra_json") if isinstance(meta, dict) else None
            if not raw:
                continue
            try:
                extra = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(extra, dict):
                self._extra_meta[node_id] = extra

    def clean_stale_files(self) -> int:
        """
        Scans all File nodes in metadata and removes any File nodes (and their edges)
        whose file_path does not exist on disk in workspace_path, or whose extension
        is no longer supported by the language adapters (e.g. stale JS/TS nodes left
        in a persisted snapshot after support was dropped).
        Returns the number of stale file nodes removed.
        """
        from src.database.parser.language_adapter import SUPPORTED_EXTENSIONS
        all_meta = self.get_all_metadata()
        stale_files = []
        for node_id, meta in all_meta.items():
            if meta.get("type") == "File":
                fpath = meta.get("file_path") or node_id
                full_path = fpath if os.path.isabs(fpath) else os.path.join(self.workspace_path, fpath)
                if not os.path.exists(full_path):
                    stale_files.append(fpath)
                elif os.path.splitext(fpath)[1].lower() not in SUPPORTED_EXTENSIONS:
                    stale_files.append(fpath)

        removed_count = 0
        for fpath in stale_files:
            try:
                self.invalidate_file(fpath)
                removed_count += 1
            except Exception as e:
                logger.debug(f"Failed to invalidate stale file {fpath}: {e}")

        if removed_count > 0:
            self.rebuild()
            logger.info(f"Cleaned {removed_count} stale/ghost File nodes from EngramDB snapshot.")
        return removed_count

    def add_node(self, node_id: str, node_type: str, name: str, file_path: str, 
                 signature: str = None, docstring: str = None, lines: dict = None, 
                 returns: list = None, calls: list = None, django_relations: list = None,
                 is_async: bool = None, is_generator: bool = None, param_count: int = None,
                 is_exported: bool = None, blast_radius_score: int = None, _extra: dict = None):
        """
        Adds a node to the metadata index in Rust.
        - calls: list of strings (function names called by this node)
        - django_relations: list of dicts (for model relations)
        """
        if _extra:
            self._extra_meta[node_id] = _extra
        
        lines_start = lines.get('start') if lines else None
        lines_end = lines.get('end') if lines else None
        
        import json
        django_json = json.dumps(django_relations) if django_relations else None
        extra_json_str = json.dumps(_extra) if _extra else None

        self.engine.add_node(
            node_id=node_id,
            node_type=node_type,
            name=name,
            file_path=file_path,
            signature=signature,
            docstring=docstring,
            lines_start=lines_start,
            lines_end=lines_end,
            returns=returns,
            calls=calls,
            django_relations_json=django_json,
            is_async=is_async,
            is_generator=is_generator,
            param_count=param_count,
            is_exported=is_exported,
            blast_radius_score=blast_radius_score,
            extra_json=extra_json_str
        )


    def add_edge(self, from_id: str, to_id: str):
        """Add an executable dependency edge between two nodes."""
        self.engine.add_edge(from_id, to_id)

    def add_structural_edge(self, from_id: str, to_id: str):
        """Add a containment, import, or data-model relationship edge."""
        self.engine.add_structural_edge(from_id, to_id)

    def add_generated_edge(self, from_id: str, to_id: str):
        """Add an executable edge replaced by the next resolution pipeline."""
        self.engine.add_generated_edge(from_id, to_id)

    def clear_generated_edges(self):
        """Drop stale route/middleware/API edges before re-resolution."""
        self.engine.clear_generated_edges()

    def resolve_and_connect_calls(self, caller_id: str, call_names: list):
        """
        Resolves multiple call names to node IDs and connects them in the graph.
        Uses the high-performance Rust O(1) name index.
        """
        return self.engine.resolve_and_connect_calls(caller_id, call_names)

    def resolve_and_connect_django(self, node_id: str, django_relations: list):
        """
        Resolves Django model names to node IDs and connects them in the graph.
        """
        import json
        return self.engine.resolve_and_connect_django(node_id, json.dumps(django_relations))

    def build(self):
        """Compile the CSR graph and save snapshot."""
        self.engine.build()

    def rebuild(self):
        """Incrementally rebuild the CSR graph and save snapshot."""
        self.engine.rebuild()

    def _index_meta_path(self) -> str:
        return os.path.join(self.workspace_path, INDEX_META_FILENAME)

    def write_index_meta(self, node_count: int = None, file_manifest: dict = None) -> None:
        """Record the indexing configuration that produced the current graph.

        Written after a full scan/rebuild so a later start can detect that the
        persisted graph no longer matches the current language config.

        file_manifest: optional {rel_path: [mtime_ns, size]} for every indexed
        source file — enables warm starts (skip re-parsing unchanged files).
        """
        import json
        import time
        from src.database.parser.language_adapter import compute_index_fingerprint, SUPPORTED_EXTENSIONS
        meta = {
            "fingerprint": compute_index_fingerprint(),
            "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "supported_extensions": list(SUPPORTED_EXTENSIONS),
            "node_count": node_count if node_count is not None else len(self.get_all_metadata()),
        }
        if file_manifest is not None:
            meta["file_manifest"] = file_manifest
        try:
            with open(self._index_meta_path(), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to write index meta: {e}")

    def load_index_meta(self) -> dict | None:
        """Return the persisted index-meta dict, or None if absent/corrupt."""
        import json
        try:
            with open(self._index_meta_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def is_index_stale(self) -> bool:
        """True when the persisted graph was built under a different indexing config.

        A missing marker (never indexed) is treated as fresh — a scan is what
        creates the marker, and until then there is nothing stale to answer from.
        """
        from src.database.parser.language_adapter import compute_index_fingerprint
        try:
            with open(self._index_meta_path(), "r", encoding="utf-8") as f:
                import json
                stored = json.load(f)
        except (OSError, ValueError):
            return False
        return stored.get("fingerprint") != compute_index_fingerprint()

    def invalidate_file(self, file_path: str):
        """Remove all nodes and edges belonging to a file from the Rust engine."""
        # Clean up Python-level extra metadata for this file
        prefix = file_path.replace('\\', '/')
        keys_to_remove = [k for k in self._extra_meta if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._extra_meta[k]
        return self.engine.invalidate_file(file_path)

    def blast_radius(self, node_id: str, max_depth: int = 0) -> list:
        """
        Run BFS blast radius on the CSR graph in Rust (recursive callers).
        max_depth: 0 = unlimited, 1 = direct callers only, etc.
        Returns list of readable node_ids affected.
        """
        try:
            return self.engine.blast_radius(node_id, max_depth)
        except Exception as e:
            logger.debug(f"Blast radius failed for {node_id}: {e}")
            return []

    def get_recursive_callees(self, node_id: str, max_depth: int = 0) -> list:
        """
        Run BFS to find all recursive callees in Rust.
        max_depth: 0 = unlimited, 1 = direct callees only, etc.
        """
        try:
            return self.engine.get_recursive_callees(node_id, max_depth)
        except Exception as e:
            logger.debug(f"Recursive callees failed for {node_id}: {e}")
            return []

    def get_callers(self, node_id: str) -> list:
        """Get direct callers of a node from the Rust graph (deduplicated, order-preserving)."""
        try:
            return _dedupe_preserve_order(self.engine.get_callers(node_id))
        except Exception as e:
            logger.debug(f"get_callers failed for {node_id}: {e}")
            return []

    def get_callees(self, node_id: str) -> list:
        """Get direct callees of a node from the Rust graph (deduplicated, order-preserving)."""
        try:
            return _dedupe_preserve_order(self.engine.get_callees(node_id))
        except Exception as e:
            logger.debug(f"get_callees failed for {node_id}: {e}")
            return []

    def get_dependents(self, node_id: str) -> list:
        """Get direct incoming relationships of every edge type."""
        try:
            return _dedupe_preserve_order(self.engine.get_dependents(node_id))
        except Exception as e:
            logger.debug(f"get_dependents failed for {node_id}: {e}")
            return []

    def get_dependencies(self, node_id: str) -> list:
        """Get direct outgoing relationships of every edge type."""
        try:
            return _dedupe_preserve_order(self.engine.get_dependencies(node_id))
        except Exception as e:
            logger.debug(f"get_dependencies failed for {node_id}: {e}")
            return []

    def repopulate_edges(self):
        """Re-resolve all call/Django edges by iterating stored metadata.
        Call after all files are indexed to pick up cross-file edges.
        """
        self.engine.repopulate_edges()

    def search(self, keyword: str) -> list:
        """
        Fuzzy search across the metadata index in Rust.
        """
        return self.engine.search(keyword)

    def get_node_meta(self, node_id: str) -> dict:
        """Get metadata for a specific node from Rust, merged with Python-level extras."""
        meta = self.engine.get_node_meta(node_id)
        if not meta:
            return None
        import json
        if "extra_json" in meta:
            try:
                extra_dict = json.loads(meta["extra_json"])
                if isinstance(extra_dict, dict):
                    meta.update(extra_dict)
            except Exception:
                pass
        extra = self._extra_meta.get(node_id, {})
        if extra:
            meta.update(extra)
        return meta

    def get_stats(self) -> dict:
        """Return node and edge counts from Rust."""
        return self.engine.get_stats()

    def contains(self, node_id: str) -> bool:
        """Checks if a node exists in the metadata index."""
        return self.engine.contains(node_id)

    def snapshot_loaded(self) -> bool:
        """Whether Rust restored a compatible snapshot for this process."""
        return bool(self.engine.snapshot_loaded())

    def get_all_metadata(self) -> dict:
        """Returns the entire metadata index, merged with Python-level extras."""
        all_meta = self.engine.get_all_metadata()
        import json
        for nid, meta in all_meta.items():
            if isinstance(meta, dict) and "extra_json" in meta:
                try:
                    extra_dict = json.loads(meta["extra_json"])
                    if isinstance(extra_dict, dict):
                        meta.update(extra_dict)
                except Exception:
                    pass
        for nid, extra in self._extra_meta.items():
            if nid in all_meta:
                all_meta[nid].update(extra)
            else:
                all_meta[nid] = dict(extra)
        return all_meta

    def add_to_extra_meta(self, node_id: str, key: str, value):
        """Add extra metadata to an existing node."""
        if node_id not in self._extra_meta:
            self._extra_meta[node_id] = {}
        self._extra_meta[node_id][key] = value

    def _build_name_index(self, meta_type: str) -> dict:
        """Build O(1) name-to-node_id index for a given type."""
        idx = {}
        for nid, meta in self.engine.get_all_metadata().items():
            if meta.get('type') == meta_type:
                idx[meta.get('name')] = nid
        return idx

    def resolve_import_edges(self):
        """Rebuild cross-file import dependencies independent of scan order.

        Importers are unchanged when an imported file is edited, so target-file
        invalidation can remove their incoming structural edges. The persisted
        Python binding map lets this pass restore those relationships globally.
        """
        all_meta = self.get_all_metadata()
        file_paths = {
            meta.get("file_path") or node_id
            for node_id, meta in all_meta.items()
            if meta.get("type") == "File"
        }

        def normalized(value: str) -> str:
            path = str(value or "").replace("\\", "/").strip("/")
            if path.endswith(".py"):
                path = path[:-3]
            if path.endswith("/__init__"):
                path = path[:-len("/__init__")]
            return path

        normalized_files = {path: normalized(path) for path in file_paths if path}

        def module_files(module: str) -> list[str]:
            wanted = normalized(module)
            if not wanted:
                return []
            exact = [path for path, norm in normalized_files.items() if norm == wanted]
            if exact:
                return exact
            suffix = [
                path for path, norm in normalized_files.items()
                if norm.endswith("/" + wanted)
            ]
            return suffix if len(suffix) == 1 else []

        edges = 0
        for importer_id, meta in all_meta.items():
            if meta.get("type") != "File":
                continue
            scopes = meta.get("python_import_bindings") or {}
            if not isinstance(scopes, dict):
                continue
            targets = set()
            for scope in scopes.values():
                if not isinstance(scope, dict):
                    continue
                for raw_binding in scope.values():
                    candidates = raw_binding if isinstance(raw_binding, list) else [raw_binding]
                    for binding in candidates:
                        if not isinstance(binding, dict):
                            continue
                        module = binding.get("module", "")
                        targets.update(module_files(module))
                        if binding.get("kind") == "from" and binding.get("symbol"):
                            targets.update(module_files(
                                f"{module}/{binding['symbol']}".strip("/")))
            for target in targets:
                if target != importer_id:
                    self.engine.add_structural_edge(target, importer_id)
                    edges += 1
        if edges:
            logger.info(f"Resolved {edges} cross-file import edges")

    def resolve_url_patterns(self):
        """Second pass: link URL patterns to their view function nodes.
        
        Also creates Route→View edges for Route nodes.
        Uses O(1) name index instead of O(N) iteration.
        """
        fn_idx = self._build_name_index('Function')
        cls_idx = self._build_name_index('Class')

        url_map = {}
        for nid, extra in list(self._extra_meta.items()):
            patterns = extra.get('url_patterns', [])
            if patterns:
                for p in patterns:
                    vname = p.get('view_name', '')
                    if vname:
                        url_map.setdefault(vname, []).append(p)

        linked = 0
        for view_name, patterns in url_map.items():
            target_id = fn_idx.get(view_name) or cls_idx.get(view_name)
            if target_id:
                existing = self._extra_meta.get(target_id, {})
                if 'url_patterns' not in existing:
                    existing['url_patterns'] = []
                existing['url_patterns'].extend(patterns)
                self._extra_meta[target_id] = existing
                linked += 1

        # ── Also connect Route nodes → View functions ──
        # Route nodes store view_name in extra meta; resolve bare name to node ID
        route_edges = 0
        for nid, extra in list(self._extra_meta.items()):
            meta = self.engine.get_node_meta(nid)
            if not meta or meta.get('type') != 'Route':
                continue
            view_name = extra.get('view_name', '')
            if not view_name:
                continue
            # Try full dotted name first, then bare last segment
            candidates = [view_name, view_name.rsplit('.', 1)[-1]]
            for candidate in candidates:
                target = fn_idx.get(candidate) or cls_idx.get(candidate)
                if target and target != nid:
                    self.engine.add_generated_edge(nid, target)
                    route_edges += 1
                    break

        if route_edges:
            logger.info(f"Created {route_edges} Route→View edges")
        logger.info(f"Resolved {linked} view nodes with URL patterns")

    def resolve_mount_prefixes(self):
        """Fourth pass: resolve add_router() mount prefixes across files.

        When a Ninja router is mounted via api.add_router("/prefix/", sub_router)
        in one file (e.g. urls.py), and the sub-router's routes are defined in
        another file (e.g. achat/api.py), this pass combines the mount prefix
        with each route URL to produce the full endpoint path.

        Supports both direct variable-name matching and import-alias resolution.
        """
        all_meta = self.get_all_metadata()

        # Step 1: Collect mount entries (add_router calls) from file extra_meta
        # mount_map: handler_var -> [(mount_url, mount_file, parent_var)]
        mount_map = {}
        # file_imports: file_node_id -> import strings
        file_imports = {}

        for file_nid, extra in list(self._extra_meta.items()):
            meta = all_meta.get(file_nid, {})
            if meta.get('type') != 'File':
                continue
            file_imports[file_nid] = extra.get('imports', [])
            url_patterns = extra.get('url_patterns', [])
            for up in url_patterns:
                if up.get('func') in ('add_router', 'include_router', 'mount', 'use'):
                    mount_url = up.get('url', '')
                    sub_router_var = up.get('sub_router_var', '') or up.get('view_name', '')
                    parent_var = up.get('parent_var', '')
                    if mount_url and sub_router_var:
                        mount_map.setdefault(sub_router_var, []).append({
                            'mount_url': mount_url,
                            'mount_file': file_nid,
                            'parent_var': parent_var,
                        })

        if not mount_map:
            return

        # Step 1b: Determine parent variable prefixes (e.g. path("api/", api.urls) -> api has prefix "/api/")
        parent_prefixes = {}
        for file_nid, extra in list(self._extra_meta.items()):
            url_patterns = extra.get('url_patterns', [])
            for up in url_patterns:
                func = up.get('func', '')
                url = up.get('url', '')
                view_name = up.get('view_name', '')
                if func == 'path' and url and view_name:
                    var_name = view_name.split('.')[0]
                    parent_prefixes[var_name] = url.strip('/')

        # Combine mount_url with parent_var prefix if present
        for sub_router_var, mounts in mount_map.items():
            for mv in mounts:
                pvar = mv.get('parent_var', '')
                pprefix = parent_prefixes.get(pvar, '')
                murl = mv['mount_url'].strip('/')
                if pprefix:
                    mv['full_mount_prefix'] = '/' + pprefix + '/' + murl
                else:
                    mv['full_mount_prefix'] = '/' + murl if murl else ''

        # Step 2: Build import alias maps per mount file
        # alias_map[mount_file][alias_var] = {'orig_var': ..., 'module': ...}
        alias_map = {}
        mount_files = {mv['mount_file']
                       for mounts in mount_map.values() for mv in mounts}
        for mf in mount_files:
            aliases = {}
            for imp in file_imports.get(mf, []):
                if not isinstance(imp, str):
                    continue
                esm = _ESM_IMPORT_RE.match(imp.strip().rstrip(';'))
                if esm is not None:
                    mod_str = esm.group('mod')
                    names = []
                    if esm.group('ns'):
                        aliases[esm.group('ns')] = {
                            'orig_var': esm.group('ns'), 'module': mod_str}
                    if esm.group('named'):
                        names.extend(esm.group('named').split(','))
                    if esm.group('default'):
                        names.append(esm.group('default'))
                    if esm.group('default'):
                        names.append(esm.group('default'))
                        if esm.group('named2'):
                            names.extend(esm.group('named2').split(','))
                    for item in names:
                        item = item.strip()
                        if not item:
                            continue
                        if ' as ' in item:
                            orig, alias = [x.strip() for x in item.split(' as ', 1)]
                        else:
                            orig = alias = item
                        aliases[alias] = {'orig_var': orig, 'module': mod_str}
                    continue
                if ' import ' in imp:
                    try:
                        parts = imp.split(' import ', 1)
                        mod_part = parts[0].replace('from ', '').strip()
                        names_part = parts[1].strip()
                        for item in names_part.split(','):
                            item = item.strip()
                            if ' as ' in item:
                                orig, alias = [x.strip() for x in item.split(' as ', 1)]
                            else:
                                orig, alias = item, item
                            aliases[alias] = {
                                'orig_var': orig,
                                'module': mod_part,
                            }
                    except Exception:
                        pass
            alias_map[mf] = aliases

        # Helper to check if imported module matches a file path
        def module_matches_file(mod_str: str, route_file: str, mount_file: str) -> bool:
            if not mod_str or not route_file:
                return False
            # './routes/usersRouter' -> 'routes/usersRouter'; '..' prefixes are
            # best-effort flattened by dropping the leading dot segments.
            mod_clean = mod_str.strip()
            while mod_clean.startswith('./'):
                mod_clean = mod_clean[2:]
            mod_path = mod_clean.strip('.').replace('.', '/')
            clean_route = route_file.replace('\\', '/').rstrip('/')
            clean_route_no_ext = clean_route
            for ext in ('.py', '.tsx', '.ts', '.jsx', '.js', '.mjs', '.cjs'):
                if clean_route_no_ext.endswith(ext):
                    clean_route_no_ext = clean_route_no_ext[:-len(ext)]
                    break

            # Package import ("from app.routers import items"): the file lives
            # INSIDE the imported package directory.
            if (clean_route_no_ext + '/').startswith(mod_path + '/'):
                return True

            mod_parts = [p for p in mod_path.split('/') if p]
            route_parts = [p for p in clean_route_no_ext.split('/') if p]

            if not mod_parts or not route_parts:
                return False

            if len(mod_parts) > 1:
                return route_parts[-len(mod_parts):] == mod_parts or mod_parts[-len(route_parts):] == route_parts
            else:
                if route_parts[-1] == mod_parts[0]:
                    mount_dir = os.path.dirname(mount_file.replace('\\', '/'))
                    route_dir = os.path.dirname(route_file.replace('\\', '/'))
                    return (not mount_file) or (mount_dir == route_dir)
                return False

        # Step 3: Match decorator-based Route nodes to mounts
        updates = 0
        for route_id, extra in list(self._extra_meta.items()):
            meta = all_meta.get(route_id, {})
            if meta.get('type') != 'Route':
                continue
            if extra.get('func') != 'decorator':
                continue

            source_var = extra.get('source_var', '')
            route_url = extra.get('url', '')
            route_file = meta.get('file_path', '')

            matched_mounts = []
            for sub_router_var, mounts in mount_map.items():
                for mv in mounts:
                    mf = mv['mount_file']
                    mf_aliases = alias_map.get(mf, {})
                    attr_part = ''
                    lookup_var = sub_router_var
                    if '.' in sub_router_var:
                        base, _, attr_part = sub_router_var.partition('.')
                        lookup_var = base if base in mf_aliases else sub_router_var
                    info = mf_aliases.get(lookup_var)

                    if info:
                        orig_var = info['orig_var']
                        mod_str = info['module']
                        var_matches = (not source_var) or (source_var == orig_var) \
                            or (attr_part and source_var == attr_part) \
                            or ('.' in orig_var and orig_var.endswith('.' + source_var))
                        file_matches = module_matches_file(mod_str, route_file, mf)
                        if var_matches and file_matches:
                            matched_mounts.append((mv, orig_var))
                    else:
                        same_file_match = (not source_var or source_var == sub_router_var
                                           or (attr_part and source_var == attr_part)) \
                            and mf == route_file
                        if same_file_match:
                            matched_mounts.append((mv, ''))

            if not matched_mounts:
                continue

            # Disambiguate: prefer the mount whose imported router name matches a
            # path segment of the route file (items.router → app/routers/items.py).
            chosen = matched_mounts[0][0]
            for _mv, _orig in matched_mounts:
                if _orig and f"/{_orig}." in "/" + route_file:
                    chosen = _mv
                    break
            mv = chosen
            mount_prefix = mv.get('full_mount_prefix', mv['mount_url'])
            
            prefix_clean = mount_prefix.strip('/')
            route_clean = route_url.strip('/')
            
            prefix_parts = [p for p in prefix_clean.split('/') if p]
            route_parts = [p for p in route_clean.split('/') if p]
            
            if prefix_parts and route_parts and prefix_parts[-1] == route_parts[0]:
                combined_parts = prefix_parts + route_parts[1:]
            else:
                combined_parts = prefix_parts + route_parts
                
            full_url = '/' + '/'.join(combined_parts)

            if full_url != route_url:
                self._extra_meta[route_id]['full_url'] = full_url
                self._extra_meta[route_id]['url'] = full_url
                updates += 1

                view_name = extra.get('view_name', '')
                if view_name and route_file:
                    func_id = f"{route_file}:{view_name}"
                    if func_id in self._extra_meta:
                        f_extra = self._extra_meta[func_id]
                        if 'api_endpoint' in f_extra:
                            f_extra['api_endpoint']['url'] = full_url
                        if 'url_patterns' in f_extra and f_extra['url_patterns']:
                            f_extra['url_patterns'][0]['url'] = full_url

        if updates:
            logger.info(
                f"Resolved mount prefixes for {updates} Route nodes"
            )

    def resolve_middleware_edges(self):
        """Fifth pass: connect Middleware nodes to the Route nodes they wrap.

        A middleware registered on a router (e.g. app.use(auth_mw)) wraps all
        routes that share the same source_var.  Creates Middleware -[APPLIES_TO]-> Route edges
        so FLOW FOR 'route:X' can trace back through the middleware chain.
        """
        all_meta = self.get_all_metadata()

        # Group Route nodes by source_var + file_path
        routes_by_var: dict[tuple[str, str], list[str]] = {}
        for nid, extra in list(self._extra_meta.items()):
            meta = all_meta.get(nid, {})
            if meta.get('type') != 'Route' or extra.get('func') != 'decorator':
                continue
            sv = extra.get('source_var', '')
            fp = meta.get('file_path', '')
            if sv:
                routes_by_var.setdefault((sv, fp), []).append(nid)

        # Collect all source_vars for global matching (middleware on "app")
        all_source_vars: set[str] = set()
        for (sv, _), _ in routes_by_var.items():
            all_source_vars.add(sv)

        edges = 0
        for mw_id, extra in list(self._extra_meta.items()):
            meta = all_meta.get(mw_id, {})
            if meta.get('type') != 'Middleware':
                continue

            mw_sv = extra.get('source_var', '')
            mw_fp = meta.get('file_path', '')
            if not mw_sv:
                continue

            # Find routes sharing same source_var in the same file
            target_routes = routes_by_var.get((mw_sv, mw_fp), [])

            # Also match global middleware (source_var = "app") against all routes
            if not target_routes and mw_sv == 'app':
                fp = mw_fp
                for (sv, rfp), route_ids in routes_by_var.items():
                    if rfp == fp:
                        target_routes.extend(route_ids)

            for route_id in target_routes:
                if route_id != mw_id:
                    self.engine.add_generated_edge(mw_id, route_id)
                    edges += 1

        if edges:
            logger.info(
                f"Created {edges} Middleware → Route APPLIES_TO edges"
            )

    def resolve_api_calls(self):
        """Third pass: cross-reference frontend HTTP calls with backend API endpoints.
        
        Uses a two-tier index:
          Tier 1 — Route nodes (from urls.py path()/re_path()) for exact URL matching.
          Tier 2 — Function/Class nodes with api_endpoint or url_patterns meta
                  (Flask/FastAPI decorators, or Django views linked via resolve_url_patterns).
        
        Frontend HTTP calls are matched to Route nodes first (precise), then
        fall back to name-based matching against function names.
        Creates edges from frontend HTTP callers to Route/Function nodes.
        """
        import re
        
        def normalize_url(url: str, file_path: str = None) -> str:
            url = url.strip().rstrip('/')
            if not url.startswith('/'):
                url = '/' + url
                
            # Strip /api prefix if present
            if url.startswith('/api/'):
                url = url[4:]
            elif url == '/api':
                url = '/'

            # Normalize path params to a single placeholder so Express (:id),
            # Django (<int:id>), and openapi ({id}) styles compare equal.
            url = re.sub(r'<[^>]+>', '{id}', url)
            url = re.sub(r':[a-zA-Z0-9_]+', '{id}', url)
            url = re.sub(r'\{[^}]*\}', '{id}', url)
                
            # If a backend file path is provided, we can prepend the module prefix
            if file_path and "src/modules/" in file_path:
                parts = file_path.split("src/modules/")
                if len(parts) > 1:
                    module = parts[1].split("/")[0]
                    # Map 'core' module to 'auth' prefix
                    prefix = "auth" if module == "core" else module
                    if not url.startswith(f"/{prefix}/") and url != f"/{prefix}":
                        url = f"/{prefix}{url}"
            return url
        
        # ── Step 1: Index Route nodes (precise URL-based matching) ──
        route_index = {}  # {normalized_url: {method: route_node_id}}
        for node_id, meta in self.engine.get_all_metadata().items():
            meta_dict = dict(meta.items()) if hasattr(meta, 'items') else meta
            if meta_dict.get('type') != 'Route':
                continue
            extra = self._extra_meta.get(node_id, {})
            url = extra.get('url', '')
            if not url:
                continue
            norm_url = normalize_url(url)
            route_index.setdefault(norm_url, {})['GET'] = node_id
        
        # ── Step 2: Index backend endpoint function/class nodes ──
        endpoint_nodes = {}  # node_id -> {methods, name, file}
        fn_name_index = {}   # function_name_lower -> list of node_ids
        
        for node_id, meta in self.engine.get_all_metadata().items():
            meta_dict = dict(meta.items()) if hasattr(meta, 'items') else meta
            ntype = meta_dict.get('type', '')
            if ntype not in ('Function', 'Class'):
                continue
            
            func_name = meta_dict.get('name', '')
            if not func_name:
                continue
            
            file_path = meta_dict.get('file_path', '')
            extra = self._extra_meta.get(node_id, {})
            api_ep = extra.get('api_endpoint', {})
            url_patterns = extra.get('url_patterns', [])
            
            # Record in function name index for Tier 3 fallback matching
            fn_name_index.setdefault(func_name.lower(), []).append(node_id)
            
            if api_ep or url_patterns:
                methods = api_ep.get('methods', []) if api_ep else []
                urls = set()
                if api_ep and api_ep.get('url'):
                    urls.add(normalize_url(api_ep['url'], file_path))
                for up in url_patterns:
                    u = up.get('url', '') or up.get('pattern', '') or ''
                    if u:
                        urls.add(normalize_url(u, file_path))
                    ep_methods = up.get('methods', [])
                    if ep_methods:
                        methods = ep_methods
                
                endpoint_nodes[node_id] = {
                    'methods': methods or ['GET'],
                    'urls': urls,
                    'name': func_name,
                    'file': file_path,
                }
        
        if not route_index and not endpoint_nodes and not fn_name_index:
            logger.info("No API endpoints or routes found to resolve")
            return
        
        # ── Step 3: Build URL→endpoint fallback index ──
        url_endpoint_index = {}
        for nid, info in endpoint_nodes.items():
            for url in info['urls']:
                url_endpoint_index.setdefault(url, {})
                for m in info['methods']:
                    url_endpoint_index[url][m.upper()] = nid
        
        # ── Step 4: Find HTTP callers and match to Route→Endpoint ──
        edges_added = 0
        http_caller_nodes = set()
        node_file_map = {}
        node_type_map = {}
        for nid, meta in self.engine.get_all_metadata().items():
            md = dict(meta.items()) if hasattr(meta, 'items') else meta
            node_file_map[nid] = md.get('file_path', '')
            node_type_map[nid] = md.get('type', '')
        
        for caller_id, extra in list(self._extra_meta.items()):
            http_calls = extra.get('http_calls', [])
            if not http_calls:
                continue
            
            for hc in http_calls:
                call_url = hc.get('url', '')
                call_method = hc.get('method', 'GET').upper()
                
                if not call_url:
                    continue
                
                norm_call_url = normalize_url(call_url)
                
                best_match = None
                best_score = -1
                
                # ── Tier 1: Match against Route nodes (precise URL) ──
                if route_index:
                    for route_url, method_map in route_index.items():
                        target_id = method_map.get(call_method) or method_map.get('GET')
                        if not target_id:
                            continue
                        if norm_call_url == route_url:
                            score = 100
                        elif norm_call_url.startswith(route_url) and len(route_url) > 5:
                            score = 50
                        elif route_url.startswith(norm_call_url) and len(norm_call_url) > 5:
                            score = 40
                        elif norm_call_url.endswith(route_url) and len(route_url) > 3:
                            score = 35
                        elif route_url.endswith(norm_call_url) and len(norm_call_url) > 3:
                            score = 30
                        elif norm_call_url in route_url or route_url in norm_call_url:
                            score = 10
                        else:
                            continue
                        if score > best_score:
                            best_score = score
                            best_match = (target_id, route_url, 'route_url')
                
                # ── Tier 2: URL-based matching against endpoint nodes ──
                if url_endpoint_index:
                    for ep_url, method_map in url_endpoint_index.items():
                        target_id = method_map.get(call_method) or method_map.get('GET') or next(iter(method_map.values()), None)
                        if not target_id:
                            continue
                        if norm_call_url == ep_url:
                            score = 100
                        elif norm_call_url.startswith(ep_url) and len(ep_url) > 5:
                            score = 50
                        elif ep_url.startswith(norm_call_url) and len(norm_call_url) > 5:
                            score = 40
                        elif norm_call_url.endswith(ep_url) and len(ep_url) > 3:
                            score = 35
                        elif ep_url.endswith(norm_call_url) and len(norm_call_url) > 3:
                            score = 30
                        elif norm_call_url in ep_url or ep_url in norm_call_url:
                            score = 10
                        else:
                            continue
                        if score > best_score:
                            best_score = score
                            best_match = (target_id, ep_url, 'endpoint_url')
                
                # ── Tier 3: Name-based matching from URL path segments ──
                raw_segments = [s for s in norm_call_url.split('/') if s and s not in ('api', 'v1', 'v2', 'v3')]
                segments = []
                for s in raw_segments:
                    clean = re.sub(r'\{[^}]*\}', '', s)
                    if clean:
                        segments.append(clean)
                name_candidates = []
                url_module = segments[0].lower().replace('-', '_').replace(' ', '_') if segments else ''
                fn_matches: dict = {}
                for idx, seg in enumerate(reversed(segments)):
                    seg_lower = seg.lower().replace('-', '_').replace(' ', '_')
                    position_boost = (len(segments) - idx) * 5
                    for ep_name_lower, ep_ids in fn_name_index.items():
                        variants = {seg_lower, seg_lower.replace('_', '')}
                        if seg_lower.endswith('ies'):
                            variants.add(seg_lower[:-3] + 'y')
                        elif seg_lower.endswith('ses'):
                            variants.add(seg_lower[:-2])
                        elif seg_lower.endswith('s') and not seg_lower.endswith('ss'):
                            variants.add(seg_lower[:-1])
                        if seg_lower.endswith('y') and len(seg_lower) > 2 and seg_lower[-2] not in 'aeiou':
                            variants.add(seg_lower[:-1] + 'ies')
                        else:
                            variants.add(seg_lower + 's')
                        best_var_score = 0
                        for variant in variants:
                            if variant == ep_name_lower:
                                s = 80 + position_boost
                                if s > best_var_score:
                                    best_var_score = s
                            elif len(variant) > 2 and variant in ep_name_lower:
                                s = 30 + position_boost + (len(variant) * 2)
                                if ep_name_lower.startswith(variant):
                                    s += 10
                                if s > best_var_score:
                                    best_var_score = s
                        if best_var_score > 0:
                            for ep_id in ep_ids:
                                fm = fn_matches.setdefault(ep_id, {'score': 0, 'segs': set()})
                                if best_var_score > fm['score']:
                                    fm['score'] = best_var_score
                                fm['segs'].add(seg_lower)
                for ep_id, info in fn_matches.items():
                    score = info['score']
                    if len(info['segs']) > 1:
                        score += 30
                    if any(kw in ep_id.lower() for kw in ('api', 'view', 'route', 'controller', 'service', 'modules')):
                        score += 15
                    ep_methods = endpoint_nodes.get(ep_id, {}).get('methods', [])
                    if call_method in ep_methods:
                        score += 25
                    ep_name = ep_id.split(':')[-1].lower().lstrip('@')
                    method_prefix_map = {
                        'GET': ('get_', 'list_', 'fetch_', 'retrieve_', 'search_', 'find_'),
                        'POST': ('create_', 'post_', 'add_', 'new_', 'insert_', 'submit_'),
                        'PUT': ('update_', 'put_', 'edit_', 'modify_', 'set_'),
                        'PATCH': ('patch_', 'update_', 'modify_'),
                        'DELETE': ('delete_', 'remove_', 'destroy_', 'del_'),
                    }
                    for prefix in method_prefix_map.get(call_method, ()):
                        if ep_name.startswith(prefix):
                            score += 20
                            break
                    ep_tail = ep_name
                    for prefix in method_prefix_map.get(call_method, ()):
                        if ep_name.startswith(prefix):
                            ep_tail = ep_name[len(prefix):]
                            break
                    segs_for_tail = segments[1:] if len(segments) > 1 else segments
                    expected_parts = []
                    for seg in segs_for_tail:
                        s = seg.lower().replace('-', '_').replace(' ', '_')
                        if s.endswith('ies'):
                            s = s[:-3] + 'y'
                        elif s.endswith('ses'):
                            s = s[:-2]
                        elif s.endswith('s') and not s.endswith('ss'):
                            s = s[:-1]
                        expected_parts.append(s)
                    expected_tail = '_'.join(expected_parts)
                    if ep_tail and expected_tail and ep_tail == expected_tail:
                        score += 40
                    ep_file = node_file_map.get(ep_id, '')
                    if ep_file:
                        if '/test' in ep_file or ep_file.startswith('test'):
                            score -= 40
                        ep_basename = ep_file.rsplit('/', 1)[-1] if '/' in ep_file else ep_file
                        if ep_basename in ('models.py', 'apps.py', 'admin.py'):
                            score -= 40
                        if ep_basename.endswith(('.tsx', '.jsx', '.vue', '.svelte')):
                            score -= 40
                        elif ep_basename.endswith(('.ts', '.js')):
                            score -= 20
                        if url_module:
                            fp_parts = ep_file.replace('\\', '/').split('/')
                            fn_module = ''
                            if 'modules' in fp_parts:
                                mi = fp_parts.index('modules')
                                fn_module = fp_parts[mi + 1] if mi + 1 < len(fp_parts) else ''
                            if fn_module == url_module:
                                score += 20
                            elif fn_module:
                                score -= 40
                    if node_type_map.get(ep_id) == 'Class':
                        score -= 40
                    name_candidates.append((ep_id, score))
                for target_id, score in name_candidates:
                    if score >= 55 and score > best_score:
                        best_score = score
                        best_match = (target_id, call_url, 'name_fallback')
                
                if best_match:
                    target_id, matched_ref, match_type = best_match
                    if caller_id != target_id:
                        self.engine.add_generated_edge(caller_id, target_id)
                        edges_added += 1
                        http_caller_nodes.add(caller_id)
                        caller_extra = self._extra_meta.get(caller_id, {})
                        if 'api_dependencies' not in caller_extra:
                            caller_extra['api_dependencies'] = []
                        deps = caller_extra['api_dependencies']
                        dep_entry = {
                            "url": call_url,
                            "method": call_method,
                            "matched_ref": matched_ref,
                            "match_type": match_type,
                            "target_node": target_id,
                            "lib": hc.get('lib', 'unknown')
                        }
                        if dep_entry not in deps:
                            deps.append(dep_entry)
                        self._extra_meta[caller_id] = caller_extra
        
        logger.info(f"Resolved {edges_added} API call edges ({len(http_caller_nodes)} caller nodes)")

    def resolve_django_relations(self):
        """Second pass: resolve Django ORM relationships and add reverse edges.
        
        Uses O(1) name index instead of O(N) iteration.
        """
        class_idx = self._build_name_index('Class')
        edges_added = 0
        for node_id, extra in list(self._extra_meta.items()):
            relations = extra.get('django_relations', [])
            if not relations:
                continue
            for rel in relations:
                target_name = rel.get('related_model', '')
                if not target_name:
                    continue
                target_id = class_idx.get(target_name)
                if target_id and target_id != node_id:
                    self.engine.add_structural_edge(target_id, node_id)
                    edges_added += 1
        logger.info(f"Resolved {edges_added} Django ORM edges")

    def query(self, raw: str) -> dict:
        """Execute a DSL query string against the graph database."""
        # pyrefly: ignore [missing-import]
        from src.query import query as _query_engine
        return _query_engine(self, raw)

    def close(self):
        """Explicitly save and close the engine."""
        self.engine.close()
        logger.info("EngramDB Rust engine closed.")
