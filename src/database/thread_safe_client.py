"""
Thread-safe wrapper for the EngramDB client.

Thread safety is now handled internally by the Rust engine via
Arc<RwLock<InnerEngineState>>. This module is a thin pass-through
for backwards compatibility.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ThreadSafeEngramClient:
    """
    Thread-safe wrapper around EngramClient.
    
    Thread safety is handled by the Rust engine's internal Arc<RwLock>.
    This wrapper is a thin pass-through for API compatibility.
    """
    
    def __init__(self, workspace_path: str = None):
        from .graph_client import EngramClient
        
        self._client = EngramClient(workspace_path)
    
    @property
    def _extra_meta(self):
        return self._client._extra_meta
    
    # Read operations
    def search(self, keyword: str) -> List[Any]:
        return self._client.search(keyword)
    
    def get_stats(self) -> Dict[str, Any]:
        return self._client.get_stats()
    
    def contains(self, node_id: str) -> bool:
        return self._client.contains(node_id)

    def snapshot_loaded(self) -> bool:
        return self._client.snapshot_loaded()
    
    def get_node_meta(self, node_id: str) -> Dict[str, Any]:
        return self._client.get_node_meta(node_id)
    
    def get_all_metadata(self) -> Dict[str, Any]:
        return self._client.get_all_metadata()
    
    def blast_radius(self, node_id: str, max_depth: int = 0) -> List[Any]:
        return self._client.blast_radius(node_id, max_depth)

    def get_recursive_callees(self, node_id: str, max_depth: int = 0) -> List[Any]:
        return self._client.get_recursive_callees(node_id, max_depth)

    def get_callers(self, node_id: str) -> List[Any]:
        return self._client.get_callers(node_id)

    def get_callees(self, node_id: str) -> List[Any]:
        return self._client.get_callees(node_id)

    def get_dependents(self, node_id: str) -> List[Any]:
        return self._client.get_dependents(node_id)

    def get_dependencies(self, node_id: str) -> List[Any]:
        return self._client.get_dependencies(node_id)
    
    # Write operations
    def add_node(self, node_id: str, node_type: str, name: str, file_path: str, 
                 signature: str = None, docstring: str = None, lines: dict = None, 
                 returns: list = None, calls: list = None, django_relations: list = None,
                 is_async: bool = None, is_generator: bool = None, param_count: int = None,
                 is_exported: bool = None, blast_radius_score: int = None, _extra: dict = None):
        return self._client.add_node(
            node_id, node_type, name, file_path, 
            signature=signature, docstring=docstring, lines=lines, 
            returns=returns, calls=calls, django_relations=django_relations,
            is_async=is_async, is_generator=is_generator, param_count=param_count,
            is_exported=is_exported, blast_radius_score=blast_radius_score, _extra=_extra
        )
    
    def add_edge(self, from_id: str, to_id: str):
        return self._client.add_edge(from_id, to_id)

    def add_structural_edge(self, from_id: str, to_id: str):
        return self._client.add_structural_edge(from_id, to_id)

    def add_generated_edge(self, from_id: str, to_id: str):
        return self._client.add_generated_edge(from_id, to_id)

    def clear_generated_edges(self):
        return self._client.clear_generated_edges()

    def add_to_extra_meta(self, node_id: str, key: str, value: Any):
        return self._client.add_to_extra_meta(node_id, key, value)
    
    def resolve_and_connect_calls(self, caller_id: str, call_names: List[str]):
        return self._client.resolve_and_connect_calls(caller_id, call_names)

    def resolve_and_connect_django(self, node_id: str, django_relations: List[Dict[str, Any]]):
        return self._client.resolve_and_connect_django(node_id, django_relations)
    
    def build(self):
        return self._client.build()
    
    def rebuild(self):
        return self._client.rebuild()
    
    def repopulate_edges(self):
        return self._client.repopulate_edges()

    def invalidate_file(self, file_path: str):
        return self._client.invalidate_file(file_path)

    def clean_stale_files(self):
        return self._client.clean_stale_files()

    def write_index_meta(self, node_count: int = None, file_manifest: dict = None):
        return self._client.write_index_meta(node_count=node_count,
                                             file_manifest=file_manifest)

    def load_index_meta(self):
        return self._client.load_index_meta()

    def is_index_stale(self):
        return self._client.is_index_stale()

    def resolve_url_patterns(self):
        return self._client.resolve_url_patterns()

    def resolve_import_edges(self):
        return self._client.resolve_import_edges()

    def resolve_mount_prefixes(self):
        return self._client.resolve_mount_prefixes()

    def resolve_middleware_edges(self):
        return self._client.resolve_middleware_edges()

    def resolve_api_calls(self):
        return self._client.resolve_api_calls()

    def resolve_django_relations(self):
        return self._client.resolve_django_relations()

    def query(self, raw: str) -> dict:
        """Execute a DSL query string against the graph database."""
        from src.query import query as _query_engine
        return _query_engine(self._client, raw)

    def close(self):
        self._client.close()


class ThreadSafeGraphDB:
    """Thread-safe wrapper for GraphDB (backwards compatible)."""
    
    def __init__(self, workspace_path: str = None):
        self.client = ThreadSafeEngramClient(workspace_path)
        self.workspace_path = workspace_path
    
    def get_network_stats(self) -> Dict[str, Any]:
        return self.client.get_stats()
    
    def query(self, raw: str) -> dict:
        """Execute a DSL query string against the graph database."""
        return self.client.query(raw)

    def close(self):
        self.client.close()


# Module-level factory
_thread_safe_db_instances = {}


def get_thread_safe_graph_db(workspace_path: str = None) -> ThreadSafeGraphDB:
    """Returns a GraphDB instance for a specific workspace path."""
    import os
    
    if workspace_path is None:
        workspace_path = os.environ.get("WORKSPACE_PATH", os.getcwd())
    
    norm_path = os.path.normpath(workspace_path)
    
    if norm_path not in _thread_safe_db_instances:
        logger.info(f"Creating new GraphDB instance for: {norm_path}")
        _thread_safe_db_instances[norm_path] = ThreadSafeGraphDB(norm_path)
    
    return _thread_safe_db_instances[norm_path]
