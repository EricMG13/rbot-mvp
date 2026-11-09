# Adapted from Final-workflow-PY tools/pr_size.py
"""PR size and copy check.

Files copied byte-identical from a pinned commit of another repository are listed in
tools/copied-from.txt and do not count toward the PR line cap. The check measures HEAD (the
listing and the files are read from commits), not the working tree.

Listing format: first line ``source <40-hex sha>``, then one row per copied file:
``<rbot path> <- <source path>`` (byte-identical, counts 0 lines) or
``edited <rbot path> <- <source path>`` (counts only its difference from the source). A source
path with the prefix ``fwpy:`` is not fetched in CI, so that row counts as new lines. A listing
with no non-blank lines means "no copies".
"""

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

LIMIT = 800
LISTING = "tools/copied-from.txt"
SKIP_PREFIXES = ("tests/fixtures/", "tests/oracles/")
LOCKFILE = "uv.lock"
SOURCE_LINE = re.compile(r"source ([0-9a-fA-F]{40})")
EDITED_PREFIX = "edited "
ARROW = " <- "
FWPY_PREFIX = "fwpy:"

Type = dict[str, str]


def _git(repo: Path, *args: str) -> bytes:
    # Fixed argv list, no shell; git is resolved from PATH.
    done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607
    return done.stdout


def _parse_listing(text: str, name: str) -> tuple[str, Type, Type] | None:
    """Return (sha, copied dest->src, edited dest->src), or None when the listing is empty.

    Rows whose source is ``fwpy:`` are dropped: they count as new lines.
    """
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    if not rows:
        return None
    match = SOURCE_LINE.fullmatch(rows[0])
    if match is None:
        raise ValueError(f"{name}: first line must be 'source <40-hex sha>', got {rows[0]!r}")
    copied: Type = {}
    edited: Type = {}
    for row in rows[1:]:
        is_edit = row.startswith(EDITED_PREFIX)
        dest, sep, src = row.removeprefix(EDITED_PREFIX).partition(ARROW)
        dest, src = dest.strip(), src.strip()
        if not sep or not dest or not src:
            raise ValueError(f"{name}: row must be '<path> <- <source path>', got {row!r}")
        if src.startswith(FWPY_PREFIX):
            continue
        (edited if is_edit else copied)[dest] = src
    return match.group(1), copied, edited


def _entries(repo: Path, rev: str, paths: list[str]) -> dict[str, tuple[str, str, str]]:
    """(mode, type, object id) of each path that exists at rev; one git call."""
    if not paths:
        return {}
    out = _git(repo, "ls-tree", "-z", rev, "--", *paths).decode("utf-8")
    found: dict[str, tuple[str, str, str]] = {}
    for record in out.split("\0"):
        if record:
            meta, name = record.split("\t", 1)
            mode, kind, oid = meta.split(" ")
            found[name] = (mode, kind, oid)
    return found


def _read_listing(repo: Path, listing: Path) -> tuple[str, Type, Type] | None:
    """The listing as committed at HEAD; None when HEAD has no such file or it is empty."""
    try:
        name = listing.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"{listing}: the listing must be a path inside {repo}") from None
    if name not in _entries(repo, "HEAD", [name]):
        return None
    return _parse_listing(_git(repo, "show", f"HEAD:{name}").decode("utf-8"), name)


def copied_mismatches(listing: Path, repo: Path) -> list[str]:
    """Listed paths whose committed entry at HEAD differs from the source commit's.

    Both the listing and every path are read from HEAD, not the working tree, so this checks
    the same committed state as ``pr_lines``. ``listing`` must name a path inside ``repo``.
    A plain row matches when the dest at HEAD and the source at the sha have equal mode, type and
    object id; a source absent at the sha requires the dest to be absent at HEAD; an edited
    row must be a blob at both; a tree is a mismatch.
    """
    parsed = _read_listing(repo, listing)
    if parsed is None:
        return []
    sha, paths, edited = parsed
    try:
        _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    except subprocess.CalledProcessError:
        raise ValueError(f"source commit {sha} is not in this repository; fetch it first") from None
    there = _entries(repo, sha, sorted({*paths.values(), *edited.values()}))
    here = _entries(repo, "HEAD", sorted({*paths, *edited}))
    bad: list[str] = []
    for dest, src in paths.items():
        if src in there:
            if there[src][1] != "blob" or here.get(dest) != there[src]:
                bad.append(dest)
        elif dest in here:
            bad.append(dest)
    for dest, src in edited.items():
        if there.get(src, ("", "", ""))[1] != "blob" or here.get(dest, ("", "", ""))[1] != "blob":
            bad.append(dest)
    return sorted(set(bad))


def _is_excluded(path: str) -> bool:
    if path.startswith(SKIP_PREFIXES) or path == LISTING:
        return True
    if path.rsplit("/", 1)[-1] == LOCKFILE:
        return True
    return False


def _edit_lines(repo: Path, sha: str, src: str, dest: str) -> int | None:
    """Added plus deleted lines of one file, HEAD against the source commit.

    None when git reports the file binary (either side), so the caller keeps its range count.
    """
    old = _git(repo, "rev-parse", f"{sha}:{src}").decode("utf-8").strip()
    new = _git(repo, "rev-parse", f"HEAD:{dest}").decode("utf-8").strip()
    if old == new:
        return 0
    out = _git(repo, "diff", "--numstat", "--no-renames", old, new)
    added, deleted, _ = out.decode("utf-8").split("\t", 2)
    return None if added == "-" or deleted == "-" else int(added) + int(deleted)


def pr_lines(base: str, repo: Path) -> int:
    """Added plus deleted lines in base...HEAD, minus copies, fixtures and lockfiles."""
    parsed = _read_listing(repo, repo / LISTING)
    sha, copied, edited = parsed if parsed else ("", {}, {})
    out = _git(repo, "diff", "--numstat", "--no-renames", "-z", f"{base}...HEAD")
    total = 0
    for record in out.decode("utf-8").split("\0"):
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        if path in copied or _is_excluded(path) or added == "-" or deleted == "-":
            continue
        own = _edit_lines(repo, sha, edited[path], path) if path in edited else None
        total += int(added) + int(deleted) if own is None else own
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR size and copy check")
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    try:
        top = _git(Path.cwd(), "rev-parse", "--show-toplevel").decode("utf-8").strip()
        repo = Path(top)
        mismatches = copied_mismatches(repo / LISTING, repo)
        total = pr_lines(args.base, repo)
    except ValueError as err:
        print(err, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as err:
        detail = err.stderr.decode("utf-8", "replace").strip() if err.stderr else ""
        print(f"git failed: {shlex.join(map(str, err.cmd))}: {detail}", file=sys.stderr)
        return 1
    print(f"counted lines: {total} (limit {LIMIT})")
    for path in mismatches:
        print(f"copy mismatch: {path}")
    return 1 if mismatches or total > LIMIT else 0


if __name__ == "__main__":
    raise SystemExit(main())
