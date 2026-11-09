import pytest

from rbot.__main__ import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as done:
        main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == "rbot 0.1.0"


def test_no_args_returns_zero() -> None:
    assert main([]) == 0
