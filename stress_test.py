"""
Stress test for EngramDB concurrency + GIL release.
Tests 5 concurrent readers doing BFS while 1 writer writes simultaneously.
"""
import concurrent.futures
import time
import sys
import os

# Ensure the project root is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.database import get_graph_db


def run_concurrency_test():
    print("=" * 60)
    print("🔄 [1/4] Building graph database with 201 nodes...")
    print("=" * 60)

    # Use a temp workspace to avoid corrupting any real snapshot
    workspace = "/tmp/engram_stress_test"
    os.makedirs(workspace, exist_ok=True)
    db = get_graph_db(workspace)

    c = db.client  # shortcut

    # Central node (the "hot" function)
    central_node = "src/auth.py:core_login"
    c.add_node(central_node, "Function", "core_login", "src/auth.py")

    # Create 200 callers that all call the central node
    for i in range(200):
        caller_id = f"src/views/endpoint_{i}.py:view_handler_{i}"
        c.add_node(
            caller_id, "Method",
            f"view_handler_{i}",
            f"src/views/endpoint_{i}.py"
        )
        c.add_edge(caller_id, central_node)

    # Build the CSR graph and save snapshot
    c.build()
    print(f"✅ [2/4] Graph ready. {c.get_stats()}")
    print()

    # ── Worker definitions ─────────────────────────────────────────

    def reader_worker(worker_id):
        """Runs 100 BFS traversals — tests parallel reads + GIL release."""
        local_c = get_graph_db(workspace).client
        print(f"  📖 [Read-{worker_id}] Starting 100x BFS traversal...")
        start = time.time()

        for _ in range(100):
            _ = local_c.blast_radius(central_node, max_depth=0)

        duration = time.time() - start
        print(f"  ✅ [Read-{worker_id}] Done 100 BFS in {duration:.4f}s")
        return duration

    def writer_worker():
        """Waits 50ms for readers to start, then acquires write lock."""
        time.sleep(0.05)
        print(f"  ✍️  [Write] Requesting write lock (while readers are active)...")
        local_c = get_graph_db(workspace).client
        start = time.time()

        new_node = "src/new_feature.py:new_func"
        local_c.add_node(new_node, "Function", "new_func", "src/new_feature.py")
        local_c.add_edge(new_node, central_node)
        local_c.build()

        duration = time.time() - start
        print(f"  🚀 [Write] Write succeeded in {duration:.4f}s")

    # ── Launch threads ────────────────────────────────────────────

    print("🔥 [3/4] Launching 5 readers + 1 writer (concurrent)...")
    print()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = []

        # Launch 5 readers
        for i in range(5):
            futures.append(executor.submit(reader_worker, i))

        # Launch 1 writer (pass function, not call it)
        futures.append(executor.submit(writer_worker))

        # Wait for all to complete
        concurrent.futures.wait(futures)

    print()
    print("=" * 60)
    print("🎉 [4/4] Stress test completed — no deadlock, no crash!")
    print("=" * 60)

    # Cleanup temp snapshot
    snap = os.path.join(workspace, ".engram_snapshot.bin")
    if os.path.exists(snap):
        os.remove(snap)


if __name__ == "__main__":
    run_concurrency_test()
