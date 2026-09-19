"""Entry point of the windowed ``CodexSync.exe``: the window, and the CLI too.

The scheduled task runs whatever ``system_scheduler.job_command()`` returns,
and in a frozen build that is ``sys.executable`` itself -- so this one exe is
started as ``CodexSync.exe -c <config> guardian snapshot --once`` every minute
or so. Being windowed is what keeps a console from flashing each time; it also
means the same file must behave as the command line whenever it is given a
command, and open the window only when it is not.

The rule is deliberately narrow: the arguments name a CLI command exactly when
their first positional token (skipping the value of an option that takes one,
such as ``-c``) is one of the command names ``cli.build_parser()`` defines.
Both sets are read from that parser, so a command added to the CLI is routed
here without anyone remembering this file.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Sequence


def ensure_standard_streams() -> None:
    """Give a windowed process somewhere to print.

    A ``console=False`` build starts with ``sys.stdout``/``sys.stderr`` set to
    ``None``: the first ``print`` raises, and a logging handler created on
    ``None`` fails on every record. Both are pointed at the null device before
    anything can print or log. Streams that exist -- a console, a redirect, a
    test runner -- are left exactly as they are.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _parser_shape(parser: argparse.ArgumentParser) -> tuple[frozenset[str], frozenset[str]]:
    """(command names, top-level option strings that consume a value)."""
    commands: set[str] = set()
    valued: set[str] = set()
    for action in parser._actions:  # argparse exposes no public walk
        if isinstance(action, argparse._SubParsersAction):
            commands.update(action.choices)
        elif action.option_strings and action.nargs != 0:
            valued.update(action.option_strings)
    return frozenset(commands), frozenset(valued)


def is_cli_invocation(argv: Sequence[str], parser: argparse.ArgumentParser | None = None) -> bool:
    if parser is None:
        from codexsync.cli import build_parser

        parser = build_parser()
    commands, valued = _parser_shape(parser)
    tokens = iter(argv)
    for token in tokens:
        if token == "--":
            token = next(tokens, None)
            return token in commands
        if token.startswith("-") and token != "-":
            # `-c value` / `--config value` consume the next token; the
            # attached spellings `--config=value` and `-cvalue` do not.
            if token in valued:
                next(tokens, None)
            continue
        return token in commands
    return False


def main(
    argv: Sequence[str] | None = None,
    *,
    cli_main: Callable[[list[str]], int] | None = None,
    gui_main: Callable[[list[str]], int] | None = None,
) -> int:
    ensure_standard_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    if is_cli_invocation(args):
        if cli_main is None:
            from codexsync.cli import main as cli_main
        try:
            return int(cli_main(args))
        except SystemExit as exc:  # argparse: --help, or a malformed command
            if exc.code is None:
                return 0
            return exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            # A windowed build shows an uncaught traceback in a modal dialog.
            # For a scheduled, headless run that dialog would wait for a click
            # nobody is there to give, so the failure becomes an exit code.
            import traceback

            traceback.print_exc()
            return 1
    if gui_main is None:
        from codexsync.gui.__main__ import main as gui_main
    return int(gui_main(args))


if __name__ == "__main__":
    ensure_standard_streams()
    raise SystemExit(main())
