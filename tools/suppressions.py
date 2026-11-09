"""Fail when suppression comments under src/ and tools/ exceed tools/suppressions-baseline.txt.

Only real comments count (tokenize), so strings that merely contain a marker are ignored.
"""

import io
import sys
import tokenize
from pathlib import Path

MARKERS = ("noqa", "type: ignore", "nosec", "pragma: no cover")
ROOTS = ("src", "tools")
BASELINE = "tools/suppressions-baseline.txt"


def count(root: Path) -> int:
    total = 0
    for sub in ROOTS:
        for path in sorted((root / sub).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT and any(m in tok.string for m in MARKERS):
                    total += 1
    return total


def main(root: Path | None = None) -> int:
    base = root or Path(__file__).resolve().parent.parent
    allowed = int((base / BASELINE).read_text(encoding="utf-8").strip())
    found = count(base)
    print(f"suppressions: {found} (baseline {allowed})")
    return 1 if found > allowed else 0


if __name__ == "__main__":
    sys.exit(main())
