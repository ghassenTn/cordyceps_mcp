"""
EngramDB-backed GraphDB wrapper.
Thread safety is handled internally by the Rust engine via Arc<RwLock>.
"""
import logging
import os
from .thread_safe_client import ThreadSafeGraphDB

logger = logging.getLogger(__name__)


class GraphDB:
    """
    GraphDB wrapper around EngramDB Rust engine.
    Thread-safe via Rust-side Arc<RwLock<InnerEngineState>>.
    """
    def __init__(self, workspace_path: str = None):
        from .graph_client import EngramClient
        try:
            self.client = EngramClient(workspace_path)
            logger.info(f"Successfully initialized EngramDB for workspace: {workspace_path}")
        except Exception as e:
            logger.error(f"Failed to initialize EngramDB: {e}")
            raise

    def close(self):
        self.client.close()

    def get_network_stats(self) -> dict:
        return self.client.get_stats()

    def query(self, raw: str) -> dict:
        """Execute a DSL query string against the graph database."""
        return self.client.query(raw)


# Global cache for database connections
_db_instances = {}


def get_graph_db(workspace_path: str = None) -> ThreadSafeGraphDB:
    """
    Returns a GraphDB instance for a specific workspace path.
    Thread-safe via Rust-side Arc<RwLock> — safe for concurrent access.
    """
    if not workspace_path:
        workspace_path = os.environ.get("WORKSPACE_PATH", os.getcwd())

    norm_path = os.path.abspath(workspace_path)

    if norm_path not in _db_instances:
        logger.info(f"Creating new GraphDB instance for: {norm_path}")
        _db_instances[norm_path] = ThreadSafeGraphDB(workspace_path=norm_path)

    return _db_instances[norm_path]