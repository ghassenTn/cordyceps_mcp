"""Workspace-keyed EngramDB clients."""
import logging
import os

logger = logging.getLogger(__name__)


class GraphDB:
    """Small ownership wrapper around one workspace's ``EngramClient``."""

    def __init__(self, workspace_path: str = None):
        from .graph_client import EngramClient
        self.workspace_path = os.path.abspath(
            workspace_path or os.environ.get("WORKSPACE_PATH", os.getcwd()))
        self.client = EngramClient(self.workspace_path)
        logger.info("Initialized EngramDB for workspace: %s", self.workspace_path)

    def close(self):
        self.client.close()

    def get_network_stats(self) -> dict:
        return self.client.get_stats()


# Global cache for database connections
_db_instances = {}


def get_graph_db(workspace_path: str = None) -> GraphDB:
    """Return the process-owned graph for an absolute workspace path."""
    if not workspace_path:
        workspace_path = os.environ.get("WORKSPACE_PATH", os.getcwd())

    norm_path = os.path.abspath(workspace_path)

    if norm_path not in _db_instances:
        logger.info(f"Creating new GraphDB instance for: {norm_path}")
        _db_instances[norm_path] = GraphDB(workspace_path=norm_path)

    return _db_instances[norm_path]
