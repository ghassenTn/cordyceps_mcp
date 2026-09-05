"""Conservative contextual call resolution for JavaScript and TypeScript.

The Rust engine's generic non-Python resolver matches the final call segment
globally, which makes ``obj.save()`` call every ``save`` in a workspace. This
module instead resolves only relationships proven by lexical scope,
imports/exports, class context, or explicit receiver types. Unknown and
ambiguous calls deliberately remain unresolved.
"""
from __future__ import annotations

import posixpath
from collections import deque
from typing import Any


JAVASCRIPT_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")


def _is_javascript(path: str) -> bool:
    return str(path or "").lower().endswith(JAVASCRIPT_EXTENSIONS)


class JavaScriptResolver:
    def __init__(self, metadata: dict[str, dict]):
        self.nodes = metadata
        self.files = {
            str(meta.get("file_path") or node_id)
            for node_id, meta in metadata.items()
            if meta.get("type") == "File"
        }
        self._module_cache: dict[tuple[str, str], str | None] = {}
        self._export_cache: dict[tuple[str, str], str | None] = {}
        self._star_export_cache: dict[str, dict[str, str | None]] = {}
        self._method_cache: dict[tuple[str, str], str | None] = {}
        self._context_cache: dict[str, tuple[set[str], dict[str, str | None]]] = {}
        self._class_ids = {
            node_id for node_id, meta in metadata.items() if meta.get("type") == "Class"
        }
        self._enclosing_classes = {}
        for node_id, meta in metadata.items():
            if meta.get("type") != "Function":
                continue
            if meta.get("javascript_lexical_this") is not True:
                continue
            file_path = str(meta.get("file_path", ""))
            if not node_id.startswith(file_path + ":"):
                continue
            parts = node_id[len(file_path) + 1:].split(".")
            matches = [
                f"{file_path}:{'.'.join(parts[:end])}"
                for end in range(1, len(parts))
                if f"{file_path}:{'.'.join(parts[:end])}" in self._class_ids
            ]
            if matches:
                self._enclosing_classes[node_id] = matches[-1]
        self._direct_bases: dict[str, list[str]] = {}
        for class_id in self._class_ids:
            class_meta = self.nodes.get(class_id) or {}
            class_file = str(class_meta.get("file_path", ""))
            targets = []
            for base in class_meta.get("base_classes") or []:
                base_name = str(base).split("implements", 1)[0].strip()
                target = self._resolve_class_type(class_file, base_name)
                if target:
                    targets.append(target)
            self._direct_bases[class_id] = list(dict.fromkeys(targets))
        self._method_owners: dict[str, list[str]] = {}
        for function_id, class_id in self._enclosing_classes.items():
            suffix = function_id[len(class_id) + 1:]
            if "." not in suffix:
                self._method_owners.setdefault(suffix, []).append(class_id)
        self._build_inheritance_intervals()

    def executable_edges(self) -> list[tuple[str, str]]:
        edges = []
        for caller_id, meta in self.nodes.items():
            if meta.get("type") != "Function" or not _is_javascript(meta.get("file_path", "")):
                continue
            calls = meta.get("javascript_raw_calls") or meta.get("calls") or []
            for call in calls:
                target = self.resolve_call(caller_id, str(call))
                if target and target != caller_id:
                    edges.append((caller_id, target))
        return list(dict.fromkeys(edges))

    def import_edges(self) -> list[tuple[str, str]]:
        """Structural ``imported file -> importer`` edges."""
        edges = []
        for importer, meta in self.nodes.items():
            if meta.get("type") != "File" or not _is_javascript(meta.get("file_path", "")):
                continue
            bindings = meta.get("javascript_import_bindings") or {}
            sources = set(meta.get("javascript_import_sources") or [])
            sources.update(binding.get("source", "") for binding in bindings.values())
            for source in sources:
                target = self.resolve_module(importer, source)
                if target and target != importer:
                    edges.append((target, importer))
        return list(dict.fromkeys(edges))

    def resolve_call(self, caller_id: str, call: str) -> str | None:
        call = call.strip()
        if not call:
            return None
        meta = self.nodes.get(caller_id) or {}
        file_path = str(meta.get("file_path", ""))
        shadowed, receiver_types = self._context_for_caller(caller_id)
        parts = [part for part in call.split(".") if part]
        if not parts:
            return None
        if call in shadowed:
            return None

        if len(parts) == 1:
            name = parts[0]
            if name == meta.get("name"):
                return caller_id
            if name in shadowed:
                return None
            lexical = self._lexical_symbol(caller_id, name)
            if lexical:
                return lexical
            local = self._local_symbol(file_path, name)
            if local:
                return local
            return self._resolve_binding(file_path, name)

        receiver, member = ".".join(parts[:-1]), parts[-1]
        if parts[0] == "this":
            enclosing = self._enclosing_class(caller_id)
            if receiver == "this" and enclosing:
                return self._method_on_class(enclosing, member, file_path)
            receiver_type = receiver_types.get(receiver)
            return self._method_on_type(file_path, receiver_type, member)
        if parts[0] == "super":
            enclosing = self._enclosing_class(caller_id)
            return self._super_method(enclosing, member, file_path) if enclosing else None

        receiver_type = receiver_types.get(receiver)
        if receiver_type:
            return self._method_on_type(file_path, receiver_type, member)
        if parts[0] in shadowed:
            return None

        bindings = (self.nodes.get(file_path) or {}).get("javascript_import_bindings") or {}
        if parts[0] in bindings:
            target = self._resolve_binding(file_path, parts[0], parts[1:])
            return target

        local_class = self._local_symbol(file_path, parts[0], kind="Class")
        if local_class:
            target = local_class
            for part in parts[1:]:
                target = self._member_on_target(target, part, file_path)
                if target is None:
                    return None
            return target
        return None

    def resolve_module(self, importer: str, source: str) -> str | None:
        key = (importer, source)
        if key in self._module_cache:
            return self._module_cache[key]
        source = str(source or "")
        if not source.startswith("."):
            self._module_cache[key] = None
            return None
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), source))
        ext = posixpath.splitext(base)[1].lower()
        if base in self.files:
            self._module_cache[key] = base
            return base
        candidates = []
        if ext:
            # NodeNext commonly writes './foo.js' while source is foo.ts.
            stem = base[:-len(ext)]
            alternates = (".ts", ".tsx", ".js", ".jsx") if ext in (".js", ".jsx") else ()
            candidates.extend(stem + suffix for suffix in alternates if stem + suffix in self.files)
        else:
            importer_ext = posixpath.splitext(importer)[1].lower()
            order = (".ts", ".tsx", ".js", ".jsx") \
                if importer_ext in (".ts", ".tsx") else (".js", ".jsx", ".ts", ".tsx")
            candidates.extend(base + suffix for suffix in order if base + suffix in self.files)
            candidates.extend(
                f"{base}/index{suffix}" for suffix in order
                if f"{base}/index{suffix}" in self.files
            )
        candidates = list(dict.fromkeys(candidates))
        result = candidates[0] if len(candidates) == 1 else None
        self._module_cache[key] = result
        return result

    def _resolve_binding(self, file_path: str, local: str,
                         members: list[str] | None = None,
                         allow_type_only: bool = False) -> str | None:
        bindings = (self.nodes.get(file_path) or {}).get("javascript_import_bindings") or {}
        binding = bindings.get(local)
        if (not isinstance(binding, dict)
                or (binding.get("type_only") and not allow_type_only)):
            return None
        module = self.resolve_module(file_path, binding.get("source", ""))
        if not module:
            return None
        members = list(members or [])
        kind = binding.get("kind")
        if kind in ("namespace", "commonjs"):
            if not members:
                return self.resolve_export(module, "default")
            target = self.resolve_export(module, members.pop(0))
        else:
            exported = binding.get("imported") or ("default" if kind == "default" else local)
            target = self.resolve_export(module, exported)
        while target and members:
            target = self._member_on_target(target, members.pop(0), module)
        return target

    def resolve_export(self, file_path: str, exported: str,
                       seen: set[tuple[str, str]] | None = None) -> str | None:
        key = (file_path, exported)
        if key in self._export_cache:
            return self._export_cache[key]
        seen = set(seen or ())
        if key in seen:
            return None
        seen.add(key)
        exports = (self.nodes.get(file_path) or {}).get("javascript_exports") or {}
        record = exports.get(exported)
        if not isinstance(record, dict):
            target = self._star_exports(file_path, seen).get(exported)
            self._export_cache[key] = target
            return target
        if record.get("kind") == "local":
            local = record.get("local", "")
            target = self._local_symbol(file_path, local)
            if target is None:
                target = self._resolve_binding(file_path, local)
        elif record.get("kind") == "reexport":
            module = self.resolve_module(file_path, record.get("source", ""))
            target = self.resolve_export(module, record.get("imported", ""), seen) if module else None
        else:
            target = None
        self._export_cache[key] = target
        return target

    def _star_exports(self, file_path: str,
                      seen: set[tuple[str, str]] | None = None) -> dict[str, str | None]:
        if file_path in self._star_export_cache:
            return self._star_export_cache[file_path]
        marker = (file_path, "*")
        seen = set(seen or ())
        if marker in seen:
            return {}
        seen.add(marker)
        exports = (self.nodes.get(file_path) or {}).get("javascript_exports") or {}
        merged: dict[str, str | None] = {}

        def merge(name: str, target: str | None):
            if not target or name == "default":
                return
            if name not in merged:
                merged[name] = target
            elif merged[name] != target:
                merged[name] = None

        stars = exports.get("*", []) if isinstance(exports.get("*"), list) else ()
        for star in stars:
            module = self.resolve_module(file_path, star.get("source", ""))
            if not module:
                continue
            module_exports = (self.nodes.get(module) or {}).get("javascript_exports") or {}
            for name, record in module_exports.items():
                if name != "*" and isinstance(record, dict):
                    merge(name, self.resolve_export(module, name, seen))
            for name, target in self._star_exports(module, seen).items():
                merge(name, target)
        self._star_export_cache[file_path] = merged
        return merged

    def _local_symbol(self, file_path: str, name: str, kind: str | None = None) -> str | None:
        node_id = f"{file_path}:{name}"
        meta = self.nodes.get(node_id)
        if not meta or meta.get("type") not in ("Function", "Class"):
            return None
        return node_id if kind is None or meta.get("type") == kind else None

    def _context_for_caller(self, caller_id: str) -> tuple[set[str], dict[str, str | None]]:
        if caller_id in self._context_cache:
            return self._context_cache[caller_id]
        meta = self.nodes.get(caller_id) or {}
        file_path = str(meta.get("file_path", ""))
        qualified = caller_id[len(file_path) + 1:] if caller_id.startswith(file_path + ":") else ""
        shadows = set()
        receivers = {}
        parts = qualified.split(".")
        for end in range(1, len(parts) + 1):
            ancestor = self.nodes.get(f"{file_path}:{'.'.join(parts[:end])}") or {}
            if ancestor.get("type") != "Function":
                continue
            ancestor_shadows = set(ancestor.get("javascript_shadowed_names") or [])
            ancestor_receivers = ancestor.get("javascript_receiver_types") or {}
            shadows.update(ancestor_shadows)
            for name in ancestor_shadows:
                if name not in ancestor_receivers:
                    receivers.pop(name, None)
            receivers.update(ancestor_receivers)
        if meta.get("javascript_lexical_this") is not True:
            receivers = {name: type_name for name, type_name in receivers.items()
                         if not name.startswith("this.")}
        self._context_cache[caller_id] = (shadows, receivers)
        return shadows, receivers

    def _lexical_symbol(self, caller_id: str, name: str) -> str | None:
        file_path = str((self.nodes.get(caller_id) or {}).get("file_path", ""))
        qualified = caller_id[len(file_path) + 1:] if caller_id.startswith(file_path + ":") else ""
        scopes = qualified.split(".")
        for end in range(len(scopes), 0, -1):
            candidate = f"{file_path}:{'.'.join(scopes[:end])}.{name}"
            if candidate in self.nodes and (self.nodes[candidate] or {}).get("type") in (
                    "Function", "Class"):
                return candidate
        return None

    def _enclosing_class(self, caller_id: str) -> str | None:
        return self._enclosing_classes.get(caller_id)

    def _member_on_target(self, target: str, member: str, context_file: str) -> str | None:
        if (self.nodes.get(target) or {}).get("type") == "Class":
            return self._method_on_class(target, member, context_file)
        return None

    def _method_on_type(self, file_path: str, type_name: str | None,
                        method: str) -> str | None:
        target = self._resolve_class_type(file_path, type_name)
        return self._method_on_class(target, method, file_path) \
            if target and (self.nodes.get(target) or {}).get("type") == "Class" else None

    def _resolve_class_type(self, file_path: str, type_name: str | None) -> str | None:
        if not type_name:
            return None
        clean = str(type_name).split("<", 1)[0].strip()
        parts = [part for part in clean.split(".") if part]
        if not parts:
            return None
        local = self._local_symbol(file_path, clean, kind="Class")
        target = local or self._resolve_binding(
            file_path, parts[0], parts[1:], allow_type_only=True)
        return target if (self.nodes.get(target) or {}).get("type") == "Class" else None

    def _base_class_ids(self, class_id: str, fallback_file: str) -> list[str]:
        if class_id in self._direct_bases:
            return self._direct_bases[class_id]
        class_meta = self.nodes.get(class_id) or {}
        class_file = str(class_meta.get("file_path") or fallback_file)
        targets = []
        for base in class_meta.get("base_classes") or []:
            base_name = str(base).split("implements", 1)[0].strip()
            target = self._resolve_class_type(class_file, base_name)
            if target:
                targets.append(target)
        return list(dict.fromkeys(targets))

    def _build_inheritance_intervals(self) -> None:
        """O(1) ancestor checks for JavaScript's normal single-extends trees."""
        self._inheritance_in = {}
        self._inheritance_out = {}
        self._inheritance_depth = {}
        children = {class_id: [] for class_id in self._class_ids}
        parent = {}
        for child, bases in self._direct_bases.items():
            if len(bases) == 1:
                parent[child] = bases[0]
                children.setdefault(bases[0], []).append(child)
        counter = 0
        visited = set()
        roots = [class_id for class_id in self._class_ids if class_id not in parent]
        for root in [*roots, *self._class_ids]:
            if root in visited:
                continue
            stack = [(root, 0, False)]
            while stack:
                node, depth, exiting = stack.pop()
                if exiting:
                    self._inheritance_out[node] = counter
                    counter += 1
                    continue
                if node in visited:
                    continue
                visited.add(node)
                self._inheritance_in[node] = counter
                self._inheritance_depth[node] = depth
                counter += 1
                stack.append((node, depth, True))
                for child in reversed(children.get(node, ())):
                    stack.append((child, depth + 1, False))

    def _is_ancestor(self, ancestor: str, child: str) -> bool:
        return (ancestor in self._inheritance_in and child in self._inheritance_in
                and self._inheritance_in[ancestor] <= self._inheritance_in[child]
                and self._inheritance_out[child] <= self._inheritance_out[ancestor])

    def _method_on_class(self, class_id: str | None, method: str,
                         context_file: str, seen=None) -> str | None:
        if not class_id:
            return None
        cache_key = (class_id, method)
        if cache_key in self._method_cache:
            return self._method_cache[cache_key]
        direct = f"{class_id}.{method}"
        if direct in self.nodes and (self.nodes[direct] or {}).get("type") == "Function":
            self._method_cache[cache_key] = direct
            return direct
        owners = self._method_owners.get(method, ())
        # Sparse owner sets are faster through O(1) ancestry intervals. For a
        # ubiquitous name such as ``save``, walking the actual base chain avoids
        # scanning thousands of unrelated classes for every call.
        inherited_owners = [
            owner for owner in owners
            if owner != class_id and self._is_ancestor(owner, class_id)
        ] if len(owners) <= 64 else []
        if inherited_owners:
            inherited_owners.sort(
                key=lambda owner: self._inheritance_depth.get(owner, -1), reverse=True)
            if (len(inherited_owners) == 1
                    or self._inheritance_depth.get(inherited_owners[0])
                    > self._inheritance_depth.get(inherited_owners[1])):
                result = f"{inherited_owners[0]}.{method}"
                self._method_cache[cache_key] = result
                return result
        visited = set(seen or ())
        queue = deque([class_id])
        parent = {class_id: None}
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            current_key = (current, method)
            if current_key in self._method_cache:
                result = self._method_cache[current_key]
                cursor = current
                while cursor is not None:
                    self._method_cache[(cursor, method)] = result
                    cursor = parent.get(cursor)
                self._method_cache[cache_key] = result
                return result
            candidate = f"{current}.{method}"
            if candidate in self.nodes and (self.nodes[candidate] or {}).get("type") == "Function":
                cursor = current
                while cursor is not None:
                    self._method_cache[(cursor, method)] = candidate
                    cursor = parent.get(cursor)
                return candidate
            for base in self._base_class_ids(current, context_file):
                if base not in parent:
                    parent[base] = current
                    queue.append(base)
        for visited_class in visited:
            self._method_cache.setdefault((visited_class, method), None)
        return None

    def _super_method(self, class_id: str, method: str, file_path: str) -> str | None:
        class_meta = self.nodes.get(class_id) or {}
        class_file = str(class_meta.get("file_path") or file_path)
        matches = []
        for base_id in self._base_class_ids(class_id, class_file):
            found = self._method_on_class(base_id, method, class_file)
            if found:
                matches.append(found)
        matches = list(dict.fromkeys(matches))
        return matches[0] if len(matches) == 1 else None
