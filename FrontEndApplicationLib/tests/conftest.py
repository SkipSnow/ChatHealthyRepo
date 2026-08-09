"""Put the library source on the path for its own tests.

chathealthy-frontend-lib is not pip-installed in the workstation venv, so
without this the tests in this directory fail at collection with
ModuleNotFoundError rather than running.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
