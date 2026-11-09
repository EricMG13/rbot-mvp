"""Test-only stand-in for `gh`: replays FAKE_GH_FIXTURE; see FAKE_GH_* env vars below."""

import os
import shutil
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    if "FAKE_GH_ARGV" in os.environ:
        Path(os.environ["FAKE_GH_ARGV"]).write_text("\n".join(sys.argv[1:]), encoding="utf-8")
    if "FAKE_GH_ENV_OUT" in os.environ:
        env_out = Path(os.environ["FAKE_GH_ENV_OUT"])
        env_out.write_text(
            "present" if "COPILOT_ALLOW_ALL" in os.environ else "absent", encoding="utf-8"
        )
    if "FAKE_GH_SLEEP" in os.environ:
        time.sleep(float(os.environ["FAKE_GH_SLEEP"]))
    if "FAKE_GH_STDERR" in os.environ:
        sys.stderr.write(os.environ["FAKE_GH_STDERR"])
    if "FAKE_GH_FIXTURE" in os.environ:
        with Path(os.environ["FAKE_GH_FIXTURE"]).open("rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
    sys.stdout.flush()
    sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
