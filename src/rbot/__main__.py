"""Command-line entry point."""

import argparse

from rbot import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rbot")
    parser.add_argument("--version", action="version", version=f"rbot {__version__}")
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
