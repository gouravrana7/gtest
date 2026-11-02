"""Put the plugin's scripts on the path. They ship as plain files, not a package."""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "coverage-gaps"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
