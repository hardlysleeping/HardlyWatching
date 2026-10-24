"""Start a sandbox on a held rate-limit pause and keep it up for the browser.

Prints the port and the URL, writes them to /tmp/hwa-hold-ui.port, then waits
until it is killed. Sandbox paths are all inside its own temp tree; port 8765 is
refused by the harness.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests import sandbox  # noqa: E402

sb = sandbox.Sandbox(seed_profile=None)
state = {
    "settings": {"hold_after_limit": True, "between_profiles": 0},
    "limit_paused": True,
    "user_paused": {"instagram": False, "tiktok": False},
    "cooldown_until": time.time() - 60,          # the window is over
    "consecutive_limits": 1,
    "jobs": [{"username": "held_example", "id": "heldjob1", "status": "queued",
              "platform": "instagram"}],
    "order": ["heldjob1"],
}
sb.state_file.parent.mkdir(parents=True, exist_ok=True)
sb.state_file.write_text(json.dumps(state))

sb.start()
sb._proc  # keep the reference alive
Path("/tmp/hwa-hold-ui.port").write_text(str(sb.port))
print("port", sb.port, flush=True)
try:
    while True:
        time.sleep(5)
finally:
    sb.stop()
