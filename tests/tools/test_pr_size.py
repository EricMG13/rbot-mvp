from pathlib import Path

from tools import pr_size


def _git(repo: Path, *args: str) -> str:
    return pr_size._git(repo, *args).decode("utf-8").strip()


def _init(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


def test_mapped_copy_counts_zero(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    sha = _commit(repo, {"a/x.py": "one\ntwo\nthree\n"})
    _commit(
        repo,
        {
            "b/y.py": "one\ntwo\nthree\n",
            "tools/copied-from.txt": f"source {sha}\nb/y.py <- a/x.py\n",
        },
    )
    assert pr_size.pr_lines(sha, repo) == 0
    assert pr_size.copied_mismatches(repo / pr_size.LISTING, repo) == []


def test_mapped_edit_counts_diff(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    sha = _commit(repo, {"a/x.py": "one\ntwo\nthree\n"})
    _commit(
        repo,
        {
            "b/y.py": "one\nTWO\nthree\n",
            "tools/copied-from.txt": f"source {sha}\nedited b/y.py <- a/x.py\n",
        },
    )
    assert pr_size.pr_lines(sha, repo) == 2


def test_fwpy_row_counts_as_new_lines(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    sha = _commit(repo, {"a/x.py": "x\n"})
    _commit(
        repo,
        {"b/y.py": "1\n2\n3\n", "tools/copied-from.txt": f"source {sha}\nb/y.py <- fwpy:t/y.py\n"},
    )
    assert pr_size.pr_lines(sha, repo) == 3
    assert pr_size.copied_mismatches(repo / pr_size.LISTING, repo) == []


def test_changed_copy_is_a_mismatch(tmp_path: Path) -> None:
    repo = _init(tmp_path)
    sha = _commit(repo, {"a/x.py": "one\n"})
    _commit(
        repo,
        {"b/y.py": "two\n", "tools/copied-from.txt": f"source {sha}\nb/y.py <- a/x.py\n"},
    )
    assert pr_size.copied_mismatches(repo / pr_size.LISTING, repo) == ["b/y.py"]
