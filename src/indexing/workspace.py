"""Workspace indexing lifecycle: initial scan, warm start, incremental sync.

``WorkspaceIndex`` owns everything that mutates the code graph for one
workspace so the MCP entrypoint only has to do two things: run
``initial_scan()`` once at start-up and call ``drain_pending_events()`` before
answering a tool call. All engine calls happen on the calling (main) thread;
the watchdog observer only enqueues paths.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.database import get_graph_db
from src.database.parser.language_adapter import LANGUAGE_ADAPTERS
from src.watcher.sync_handler import GraphSyncHandler, get_sync_queue

logger = logging.getLogger(__name__)

# Extensions whose edits can change cross-file edges (imports, calls, routes).
# Body-only files (json, md, ...) never do, so their events skip re-linking.
CODE_EXTENSIONS = tuple(sorted(LANGUAGE_ADAPTERS))


class WorkspaceIndex:
    """Builds and keeps the code graph of one workspace up to date."""

    def __init__(self, workspace_path: str):
        self.workspace_path = os.path.abspath(workspace_path)
        self.db = get_graph_db(self.workspace_path)
        self.handler = GraphSyncHandler(self.workspace_path)
        self.sync_errors: set[str] = set()
        self._retry_events: list[tuple[str, str]] = []
        self.node_count = 0

    @property
    def client(self) -> Any:
        return self.db.client

    # ── health (surfaced in every tool response) ─────────────────────

    def health_warnings(self) -> list[str]:
        if self.sync_errors:
            ordered = sorted(self.sync_errors)
            head = ordered[:3]
            more = len(self.sync_errors) - len(head)
            return [f"Indexing failed for: {', '.join(head)}"
                    + (f" (+{more} more)" if more else "") + "; results for those files may be stale."]
        return []

    # ── discovery ────────────────────────────────────────────────────

    def collect_source_files(self) -> list[str]:
        """Absolute paths of every indexable file, honouring the shared exclusion policy."""
        files: list[str] = []
        excluded = self.handler._excluded
        for root, dirs, names in os.walk(self.workspace_path):
            dirs[:] = [d for d in dirs if not d.startswith('.')
                       and not GraphSyncHandler.is_excluded_dir(d, os.path.join(root, d), excluded)]
            for name in names:
                if not name.lower().endswith(self.handler.supported_extensions):
                    continue
                path = os.path.join(root, name)
                if (GraphSyncHandler.is_internal_artifact(path)
                        or self.handler._is_excluded(path)):
                    continue
                files.append(path)
        return files

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self.workspace_path).replace(os.sep, "/")

    def _manifest(self, files: list[str]) -> dict[str, list[int]]:
        manifest: dict[str, list[int]] = {}
        for p in files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            manifest[self._rel(p)] = [st.st_mtime_ns, st.st_size]
        return manifest

    # ── cross-file resolution ────────────────────────────────────────

    def resolve_cross_file_edges(self) -> None:
        """Re-derive every edge that depends on more than one file.

        Order matters: generated (route/HTTP/middleware) edges are dropped,
        call edges re-resolved from stored metadata, then import, ORM, URL,
        mount-prefix, middleware and API-call passes rebuild the rest.
        """
        c = self.client
        c.clear_generated_edges()
        c.repopulate_edges()
        c.resolve_import_edges()
        c.resolve_javascript_calls()
        c.resolve_django_relations()
        c.resolve_url_patterns()
        c.resolve_mount_prefixes()
        c.resolve_middleware_edges()
        c.resolve_api_calls()
        c.persist_extra_metadata()

    # ── start-up ─────────────────────────────────────────────────────

    def initial_scan(self) -> dict:
        """Index the workspace (or trust a clean persisted snapshot) and build the graph."""
        files = self.collect_source_files()
        current = self._manifest(files)
        warmed = self._try_warm_start(current)

        if warmed:
            stats = self.client.get_stats()
            expected = (self.client.load_index_meta() or {}).get("node_count")
            drift = isinstance(expected, int) and stats["nodes"] != expected
            if drift:
                logger.warning("Snapshot drift (%s nodes vs %s expected); falling back to full scan",
                               stats["nodes"], expected)
                warmed = False
            else:
                self.client.write_index_meta(node_count=stats["nodes"], file_manifest=current)

        if not warmed:
            # Write-ahead safety: any interruption from this point may leave a
            # partially mutated engine. A clean marker is published only by
            # ``_publish`` after the complete graph is synchronously saved.
            self._mark_index_dirty()
            failed: set[str] = set()
            if files:
                failed = self._full_parse(files)
            current = self._manifest(files)
            failed.update(self._manifest_mismatches(current))
            for rel in failed:
                current.pop(rel, None)
            self.sync_errors = failed
            self._retry_events = [
                ("update", os.path.join(self.workspace_path, rel)) for rel in sorted(failed)
                if rel != "<graph-rebuild>" and os.path.exists(os.path.join(self.workspace_path, rel))
            ]
            self._clean_unindexed_files({self._rel(path) for path in files})
            self.resolve_cross_file_edges()
            self._publish(current)

        stats = self.client.get_stats()
        self.node_count = stats.get("nodes", 0)
        logger.info("Index ready: %s nodes (%s)", self.node_count, "warm start" if warmed else "full scan")
        return {"nodes": self.node_count, "files": len(files), "warm_start": warmed}

    def _try_warm_start(self, current: dict) -> bool:
        """Reuse the persisted snapshot only when NOTHING changed on disk.

        The engine restores a snapshot for reads, but its first mutation starts
        an in-memory session seeded empty, so a dirty workspace must take the
        full-scan path instead of patching individual files.
        """
        c = self.client
        meta = c.load_index_meta()
        snapshot = c.snapshot_path()
        if not (os.path.exists(snapshot) and c.snapshot_loaded() and meta
                and isinstance(meta.get("file_manifest"), dict)
                and not c.is_index_stale()):
            return False
        saved = meta["file_manifest"]
        changed = [r for r, sig in current.items() if saved.get(r) != sig]
        removed = [r for r in saved if r not in current]
        if changed or removed:
            logger.info("Warm start skipped: %d changed / %d removed files", len(changed), len(removed))
            return False
        stats = c.get_stats()
        if stats.get("nodes") != meta.get("node_count"):
            return False
        all_meta = c.get_all_metadata()
        for rel, signature in current.items():
            if (all_meta.get(rel) or {}).get("source_signature") != signature:
                return False
        logger.info("Warm start: %d unchanged files", len(current))
        return True

    def _full_parse(self, files: list[str]) -> set[str]:
        workers = os.cpu_count() or 4
        logger.info("Parsing %d files with %d workers...", len(files), workers)
        parsers = threading.local()

        def parse_one(path: str):
            try:
                before = os.stat(path)
                parser = getattr(parsers, "parser", None)
                if parser is None:
                    from src.database.parser.ast_parser import UniversalCodeParser
                    parser = parsers.parser = UniversalCodeParser()
                data = parser.parse_file(path)
                after = os.stat(path)
                signature = [after.st_mtime_ns, after.st_size]
                if signature != [before.st_mtime_ns, before.st_size]:
                    raise RuntimeError("file changed while it was being parsed")
                data["_source_signature"] = signature
                return path, data
            except Exception as e:  # noqa: BLE001 - reported per file
                return path, e

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(parse_one, files))

        errors: set[str] = set()
        for path, data in results:
            if isinstance(data, Exception):
                logger.debug("Failed to parse %s: %s", path, data)
                errors.add(self._rel(path))
                continue
            try:
                updated = self.handler.update_file_in_graph(
                    path, skip_rebuild=True, pre_parsed_data=data)
                if updated is False:
                    errors.add(self._rel(path))
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to index %s: %s", path, e)
                errors.add(self._rel(path))
        return errors

    def _manifest_mismatches(self, manifest: dict[str, list[int]]) -> set[str]:
        """Files whose indexed content signature differs from the disk manifest."""
        all_meta = self.client.get_all_metadata()
        return {rel for rel, signature in manifest.items()
                if (all_meta.get(rel) or {}).get("source_signature") != signature}

    def _clean_unindexed_files(self, indexed_paths: set[str]) -> None:
        """Remove files now excluded by policy (including legacy ``.d.ts`` nodes)."""
        for node_id, meta in list(self.client.get_all_metadata().items()):
            if meta.get("type") != "File":
                continue
            file_path = str(meta.get("file_path") or node_id)
            if file_path not in indexed_paths:
                self.client.invalidate_file(file_path)

    def _publish(self, manifest: dict[str, list[int]]) -> None:
        """Compile and synchronously persist the graph before publishing its manifest."""
        self.client.build()
        self.client.save()
        self.client.write_index_meta(
            node_count=self.client.get_stats().get("nodes", 0), file_manifest=manifest)

    def _mark_index_dirty(self) -> None:
        """Prevent a partially mutated graph from being trusted on restart."""
        previous = self.client.load_index_meta() or {}
        self.client.write_index_meta(
            node_count=previous.get("node_count"),
            file_manifest=previous.get("file_manifest"),
            dirty=True,
        )

    # ── incremental sync ─────────────────────────────────────────────

    def drain_pending_events(self) -> int:
        """Apply queued file events, re-link if needed, rebuild. Returns #events."""
        sync_queue = get_sync_queue()
        events = self._retry_events
        self._retry_events = []
        while True:
            try:
                events.append(sync_queue.get_nowait())
            except queue.Empty:
                break
        if not events:
            return 0

        # Mark dirty before the first mutation so BaseException/process
        # interruption cannot pair a partial snapshot with a clean sidecar.
        self._mark_index_dirty()
        errors = set(self.sync_errors)
        retry: list[tuple[str, str]] = []
        relink = False
        for action, path in events:
            rel_path = self._rel(path)
            try:
                if action == "update":
                    updated = self.handler.update_file_in_graph(path, skip_rebuild=True)
                    if updated is False:
                        errors.add(rel_path)
                        retry.append((action, path))
                    else:
                        errors.discard(rel_path)
                elif action == "delete":
                    self.handler.remove_file_from_graph(path, skip_rebuild=True)
                    errors.discard(rel_path)
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to %s %s: %s", action, path, e)
                errors.add(rel_path)
                retry.append((action, path))
            if path.lower().endswith(CODE_EXTENSIONS):
                relink = True

        try:
            if relink:
                self.resolve_cross_file_edges()
            self.client.rebuild()
            self.client.save()
            errors.discard("<graph-rebuild>")
        except Exception:
            errors.add("<graph-rebuild>")
            retry = events
            self._retry_events = list(dict.fromkeys(events))
            self.sync_errors = errors
            self._mark_index_dirty()
            raise

        self._retry_events = list(dict.fromkeys(retry))
        self.sync_errors = errors
        if not errors:
            files = self.collect_source_files()
            self.client.write_index_meta(
                node_count=self.client.get_stats().get("nodes", 0),
                file_manifest=self._manifest(files))
        else:
            self._mark_index_dirty()
        return len(events)

    # ── watching ─────────────────────────────────────────────────────

    def start_watching(self):
        """Start a polling observer that feeds the sync queue. Caller stops it."""
        from watchdog.observers.polling import PollingObserver
        observer = PollingObserver()
        observer.schedule(self.handler, self.workspace_path, recursive=True)
        observer.start()
        return observer
