"""Terminal output for pyjobby's command-line tools.

One home for the things every CLI surface prints with: the colour codes, the
success/warning/error lines, the table renderer, and the failure exit that
guarantees a command can never report a problem while exiting 0.

It lives here rather than in ``cli.py`` because ``bench.py`` is a second
entry point with the same output contract -- an operator reading
``pj-bench`` output and ``pj-admin`` output should not be able to tell that
two modules wrote them, and a copy in each is how they drift.
"""

from __future__ import annotations

import re
from typing import NoReturn

import click


# ANSI color codes for terminal output
class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def print_success(msg: str) -> None:
    """Print success message in green"""
    click.echo(f"{Colors.OKGREEN}{msg}{Colors.ENDC}")


def print_error(msg: str) -> None:
    """Print error message in red"""
    click.echo(f"{Colors.FAIL}Error: {msg}{Colors.ENDC}", err=True)


def print_warning(msg: str, *, err: bool = False) -> None:
    """Print warning message in yellow.

    ``err=True`` sends it to stderr, which is what a warning ABOUT a command
    that is still going to succeed wants: the command's stdout is its result,
    and a script parsing that result must not have advice mixed into it.
    """
    click.echo(f"{Colors.WARNING}{msg}{Colors.ENDC}", err=err)


#: ANSI SGR sequences (the color codes Colors.* emit). Table layout must
#: measure what the TERMINAL shows, not what Python stores: a colored "ok"
#: is two visible characters wrapped in ~13 invisible ones, and measuring
#: with len() both over-pads its column and lets the truncating slice cut
#: an escape sequence in half, bleeding color into the rest of the table.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _fit(cell: str, width: int) -> str:
    """`cell` padded (or truncated) to `width` VISIBLE characters."""
    plain = _ANSI_SGR.sub("", cell)
    if len(plain) > width:
        # Truncation drops the color rather than risk slicing a code.
        return plain[:width]
    return cell + " " * (width - len(plain))


#: What ``print_table`` puts between two columns, and therefore the width the
#: budget below has to reserve for each gap.
_COLUMN_GAP = "  "


def print_table(headers: list[str], rows: list[list[str]], max_width: int = 80) -> None:
    """Print data as a formatted table, shrinking only what overflows.

    Columns are sized to their widest VISIBLE cell, and nothing is truncated
    while the whole row fits ``max_width``. When it does not, width is taken
    back one character at a time from whichever column is currently widest,
    and never below its HEADER -- a column narrower than the word above it is
    unreadable in a way that no amount of saved space pays for.

    The rule this replaced was a flat ``max_width // len(headers)`` cap
    applied to every column whether or not the row overflowed at all. It cost
    nothing to be wrong about the total width and everything to be wrong about
    the distribution: `queues stats` has eleven numeric columns and one
    ``Limits`` column that holds a sentence, so the sentence was cut to the
    same fourteen characters as ``Cancelled`` -- on an 80-column budget the
    real row was under 70 and needed no cutting whatsoever. Worse, headers
    were cut too: ``Scheduled`` came out as ``Sched``.
    """
    if not rows:
        print_warning("No data to display")
        return

    # The narrowest a column may become, and the width it wants.
    floors = [len(h) for h in headers]
    col_widths = list(floors)
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(_ANSI_SGR.sub("", str(cell))))

    # Everything left for the columns themselves once the gaps are paid for.
    budget = max_width - len(_COLUMN_GAP) * (len(headers) - 1)

    # Take from the widest column, repeatedly, until it fits -- so the column
    # that caused the overflow is the one that pays for it. A table whose
    # HEADERS alone exceed the budget overflows instead: there is nothing left
    # to give back, and a truncated header names no column at all.
    while sum(col_widths) > budget:
        shrinkable = [i for i, w in enumerate(col_widths) if w > floors[i]]
        if not shrinkable:
            break
        col_widths[max(shrinkable, key=lambda i: col_widths[i])] -= 1

    header_row = _COLUMN_GAP.join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    click.echo(f"{Colors.BOLD}{header_row}{Colors.ENDC}")
    click.echo("-" * len(header_row))

    for row in rows:
        row_str = _COLUMN_GAP.join(
            _fit(str(cell), col_widths[i]) for i, cell in enumerate(row)
        )
        click.echo(row_str)


class ConfigProblem(SystemExit):
    """The operator's configuration is wrong -- not the database.

    A SystemExit subclass, so every caller that simply lets it propagate
    exits exactly as before; callers that need to tell the two apart (the
    doctor's per-subsystem report) can catch this specifically.
    """


class DatabaseProblem(SystemExit):
    """The database could not be reached, or refused the operation."""


def fail(
    *messages: str, code: int = 1, problem: type[SystemExit] = SystemExit
) -> NoReturn:
    """Report an operator-facing failure and exit non-zero.

    Every failure path goes through here so a command can never report a
    problem while exiting 0 — scripts chaining `pj-admin ... && next-step`
    depend on that.

    ``code`` is the exit status: 1 for an operation that could not be done,
    2 for arguments that were wrong before anything was attempted (click's
    own usage errors already exit 2, so a mistyped value reports the same
    status whether click or pyjobby caught it).
    """
    for message in messages:
        print_error(message)
    raise problem(code)
