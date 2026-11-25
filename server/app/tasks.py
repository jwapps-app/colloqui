"""Task lines: the checklist grammar inside message bodies. The ONLY place it
is spelled out on the server. (The web client's copy is static/js/tasks.js.)

A task line is a line that starts with `[ ] ` (open) or `[x] ` (done). The
box can be ticked by any channel member; nothing else about the line changes.
An OPEN TASK is an unchecked line that also has some text after the box: that
is what the sidebar counts and what the Tasks list shows.
"""
import re

from sqlalchemy import ColumnElement, func

_TASK_LINE = re.compile(r"^\[( |x)\] ", re.IGNORECASE)
_OPEN_TASK = re.compile(r"^\[ \] .")
_BOX_WIDTH = 4  # len("[ ] ")


class NoSuchLine(ValueError):
    pass


class NotATaskLine(ValueError):
    pass


def is_task_line(line: str) -> bool:
    return _TASK_LINE.match(line) is not None


def open_tasks(content: str) -> list[tuple[int, str]]:
    """(line index, text after the box) for each open task in a message body."""
    return [
        (i, line[_BOX_WIDTH:])
        for i, line in enumerate(content.split("\n"))
        if _OPEN_TASK.match(line)
    ]


def set_checked(content: str, line: int, checked: bool) -> str:
    """The body with one task line's box ticked or cleared. Only the box
    changes, never the text."""
    lines = content.split("\n")
    if line >= len(lines):
        raise NoSuchLine(line)
    if not is_task_line(lines[line]):
        raise NotATaskLine(line)
    lines[line] = ("[x] " if checked else "[ ] ") + lines[line][_BOX_WIDTH:]
    return "\n".join(lines)


# ---- the same grammar, in SQL ----


def may_have_open_task(column) -> ColumnElement[bool]:
    """Cheap prefilter: bodies that could contain an open task. Always pair it
    with open_task_count() or open_tasks(), which apply the real rule."""
    return column.ilike("%[ ] %")


def open_task_count(column) -> ColumnElement[int]:
    """Number of open tasks in a body, counted by Postgres (15+). The "n" flag
    makes ^ anchor at each line start and keeps . from crossing a newline, so
    this matches _OPEN_TASK line for line."""
    return func.regexp_count(column, r"^\[ \] .", 1, "n")
