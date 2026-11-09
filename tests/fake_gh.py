"""Test-only stand-in for `gh`. Options precede `copilot` in argv (the child env is allowlisted).

--fixture F replays F to stdout; --exit N; --stderr TEXT; --sleep S (after output, flushed);
--argv-out F writes the argv after `copilot`; --env-out F writes the child's env var names.
"""

import os
import shutil
import sys
import time
from pathlib import Path


def main(args: list[str]) -> int:
    opts: dict[str, str] = {}
    rest = args
    while rest and rest[0] != "copilot":
        opts[rest[0]] = rest[1]
        rest = rest[2:]
    if "--argv-out" in opts:
        Path(opts["--argv-out"]).write_text("\n".join(rest[1:]), encoding="utf-8", newline="\n")
    if "--env-out" in opts:
        Path(opts["--env-out"]).write_text(
            "\n".join(sorted(os.environ)), encoding="utf-8", newline="\n"
        )
    if "--stderr" in opts:
        sys.stderr.buffer.write(opts["--stderr"].encode("utf-8"))
        sys.stderr.flush()
    if "--fixture" in opts:
        with Path(opts["--fixture"]).open("rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
    sys.stdout.flush()
    if "--sleep" in opts:
        time.sleep(float(opts["--sleep"]))
    return int(opts.get("--exit", "0"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
