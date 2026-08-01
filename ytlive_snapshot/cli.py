"""Unified command-line entry point for YouTube Live Snapshot."""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from . import __version__


HELP = """usage: ytlive-snapshot <command> [options]

Schedule still-image captures from YouTube Live and render visual archive outputs.

commands:
  capture   capture once, on a test interval, or on a daily schedule
  render    create monthly and annual visual summaries from saved snapshots

options:
  -h, --help     show this help message
  --version      show the installed version

Run 'ytlive-snapshot <command> --help' for command-specific options.
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(HELP, end="")
        return 0
    if args[0] == "--version":
        print(__version__)
        return 0

    command, command_args = args[0], args[1:]
    if command == "capture":
        from . import capture

        result = capture.main(command_args, prog="ytlive-snapshot capture")
        return 0 if result is None else int(result)
    if command == "render":
        from . import render

        result = render.main(
            command_args,
            prog="ytlive-snapshot render",
        )
        return 0 if result is None else int(result)

    print(f"ytlive-snapshot: unknown command: {command}", file=sys.stderr)
    print("Try 'ytlive-snapshot --help'.", file=sys.stderr)
    return 2
