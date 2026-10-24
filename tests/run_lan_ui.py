"""Start a sandbox and stay up so the browser can look at the phone card."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import sandbox  # noqa: E402

sb = sandbox.Sandbox(seed_profile="sandbox_seed")
sb.start()
Path("/tmp/hwa-lan-ui.port").write_text(str(sb.port))
print("port", sb.port, flush=True)
try:
    while True:
        time.sleep(5)
finally:
    sb.stop()
