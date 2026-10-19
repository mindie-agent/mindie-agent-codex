"""Run real configuration writes with the native service-manager boundary isolated."""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(sys.argv.pop(1))))
import bridge
import diagnostic_support

with patch("mindie_diagnostics.reporting_commands.ensure", return_value={"status": "running"}), \
        patch.object(bridge, "_activate_reporting", side_effect=diagnostic_support.activate_reporting):
    raise SystemExit(bridge.main())
