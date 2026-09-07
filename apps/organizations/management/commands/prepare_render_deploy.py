"""Prepare a Render deployment without relying on shell control operators."""

from __future__ import annotations

import time
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError


class Command(BaseCommand):
    help = "Run production migrations and synchronize the configured Render owner."

    max_attempts = 20
    retry_seconds = 15

    def handle(self, *args: Any, **options: Any) -> None:
        verbosity = int(options["verbosity"])

        for attempt in range(1, self.max_attempts + 1):
            try:
                call_command("migrate", interactive=False, verbosity=verbosity)
            except OperationalError as exc:
                if attempt == self.max_attempts:
                    raise CommandError(
                        "The database never became reachable during Render pre-deploy."
                    ) from exc

                self.stdout.write(
                    f"Database not reachable (attempt {attempt} of {self.max_attempts}); "
                    f"waiting {self.retry_seconds}s."
                )
                time.sleep(self.retry_seconds)
            else:
                break

        call_command("bootstrap_render_access", verbosity=verbosity)
