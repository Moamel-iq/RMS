"""
Console output that cannot destroy a seed.

Arabic is this project's source language, so command output naturally contains
it, and Windows is the approved development platform (ADR-010). A Windows
console defaults to a legacy code page — `cp1252` here — which cannot encode a
single Arabic character. `self.stdout.write("  + KG  كيلوغرام")` then raises
`UnicodeEncodeError`.

That would be a cosmetic problem except for what surrounds it. Seed commands
are `@transaction.atomic`, so an exception raised while *printing* the eleventh
unit rolls back the ten already written. A fresh install reports a crash and
leaves an empty table — and the failure is in the reporting, not in the work.

So: make the stream carry UTF-8 where it can, and make a write that still
fails harmless rather than fatal. Data is the deliverable; the log line is not.
"""

from __future__ import annotations

import contextlib
from typing import Any

from django.core.management.base import BaseCommand


def use_utf8(stream: Any) -> None:
    """
    Ask a stream to carry UTF-8, quietly accepting that it may refuse.

    Django wraps the real stream in an `OutputWrapper`, so the reconfigurable
    object is the one underneath. `backslashreplace` keeps an un-encodable
    character visible as an escape rather than losing it silently.
    """
    inner = getattr(stream, "_out", stream)
    reconfigure = getattr(inner, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(Exception):
        reconfigure(encoding="utf-8", errors="backslashreplace")


class SeedCommand(BaseCommand):
    """
    A command whose output may contain Arabic and whose work must survive it.

    Subclass this instead of `BaseCommand` for anything that writes reference
    data. `write` here never raises, so a console that cannot render a name
    costs a log line and not the transaction.
    """

    def execute(self, *args: Any, **options: Any) -> Any:
        use_utf8(self.stdout)
        use_utf8(self.stderr)
        return super().execute(*args, **options)

    def write(self, message: str) -> None:
        """
        Print, or give up on printing. Never raise.

        The fallback strips the message down to something the console can
        actually accept, because a mangled line is better than a rolled-back
        seed and far better than a traceback that hides which one happened.
        """
        try:
            self.stdout.write(message)
        except UnicodeEncodeError:
            encoding = getattr(self.stdout, "encoding", None) or "ascii"
            self.stdout.write(message.encode(encoding, "replace").decode(encoding))
