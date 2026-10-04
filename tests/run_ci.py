"""Run each component case once, with public-boundary checks first."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIRST = (
    "test_public_projection",
    "test_runtime_compatibility", "test_windows_process_tree",
    "test_windows_consent_lock", "test_unicode_install", "test_protocol_encoding",
    "test_service_handoff", "test_service_entry_lifetime",
    "test_product_flow", "test_organizer_model", "test_stop_hook",
)


if __name__ == "__main__":
    available = {p.stem for p in (ROOT / "tests").glob("test_*.py")}
    missing = set(FIRST) - available
    if missing:
        raise SystemExit("Missing required contract module: " + ", ".join(sorted(missing)))
    ordered = [*FIRST, *sorted(available - set(FIRST))]
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName("tests." + name)
        for name in ordered
    )
    result = unittest.TextTestRunner(verbosity=2, failfast=False).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
