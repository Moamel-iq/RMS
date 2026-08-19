r"""
Populate a development database with the Accounting demo dataset.

    .venv\Scripts\python.exe manage.py seed_accounting_demo --user moamel

`settings.DEBUG` is checked **first**, before any argument is read, and no flag
turns it off. A demo cashbox in production would be indistinguishable from a
real drawer on every screen the business opens.

Idempotent: a second run reports `0 created, N reused`. There is no `--reset`,
for the reason `seed_sales_demo` records — everything an accounting demo
touches is or becomes ledger history, and a flag that could erase it would
promise more than it should.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import CommandError, CommandParser

from apps.accounting.demo import DemoPreconditionError, seed_accounting_demo
from apps.core.console import SeedCommand
from apps.core.context import audit_context
from apps.users.models import User


class Command(SeedCommand):
    help = "Seed the Accounting demo dataset (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--user",
            required=True,
            help="Username to record as the actor on every audit event.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        # First, before any argument is read.
        if not settings.DEBUG:
            raise CommandError("seed_accounting_demo runs only with DEBUG=True.")

        username = options["user"]
        actor = User.objects.filter(username=username).first()
        if actor is None:
            raise CommandError(f"no user named {username}")

        try:
            with audit_context(actor=actor):
                result = seed_accounting_demo()
        except DemoPreconditionError as missing:
            raise CommandError(str(missing)) from missing

        for note in result.notes:
            self.write(f"  {note}")
        self.write(f"{result.created} created, {result.reused} reused.")
