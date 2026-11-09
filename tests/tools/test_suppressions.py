from pathlib import Path

from tools import suppressions


def _setup(root: Path, source: str, baseline: str) -> None:
    (root / "src").mkdir()
    (root / "tools").mkdir()
    (root / "src" / "m.py").write_text(source, encoding="utf-8", newline="\n")
    (root / "tools" / "suppressions-baseline.txt").write_text(baseline, encoding="utf-8")


def test_suppression_over_baseline_fails(tmp_path: Path) -> None:
    _setup(tmp_path, "x = 1  # noqa\n", "0\n")
    assert suppressions.main(tmp_path) == 1


def test_suppression_at_baseline_passes(tmp_path: Path) -> None:
    _setup(tmp_path, "x = 1  # noqa\n", "1\n")
    assert suppressions.main(tmp_path) == 0


def test_string_is_not_a_suppression(tmp_path: Path) -> None:
    _setup(tmp_path, 's = "# noqa"\n', "0\n")
    assert suppressions.main(tmp_path) == 0
