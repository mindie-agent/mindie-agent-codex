"""Keep standing-host integration selectable outside the unit CI runner."""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live_npu: standing A2/A3 integration")


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name == "test_live_npu_acceptance.py":
            item.add_marker(pytest.mark.live_npu)
