"""
Pytest configuration and shared fixtures.
"""
import pytest
import tempfile
import shutil
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def workspace_root():
    """Get the workspace root directory."""
    return os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def isolated_temp_dir():
    """Create and cleanup isolated temp directory for each test."""
    tmpdir = tempfile.mkdtemp(prefix="test_graph_code_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def python_test_file(isolated_temp_dir):
    """Create a Python test file."""
    test_file = os.path.join(isolated_temp_dir, "test_module.py")
    with open(test_file, 'w') as f:
        f.write("""def simple_function():
    return 42

class SimpleClass:
    def method(self):
        return "test"

def another_function(a, b):
    return a + b
""")
    return test_file


@pytest.fixture
def js_test_file(isolated_temp_dir):
    """Create a JavaScript test file."""
    test_file = os.path.join(isolated_temp_dir, "test_module.js")
    with open(test_file, 'w') as f:
        f.write("""function simpleFunction() {
    return 42;
}

class SimpleClass {
    method() {
        return "test";
    }
}

const anotherFunction = (a, b) => a + b;
""")
    return test_file


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow tests")
    config.addinivalue_line("markers", "thread_safety: Thread safety tests")
