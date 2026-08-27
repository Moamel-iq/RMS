"""Run one bounded batch of durable automation messages."""

from __future__ import annotations

import json
import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.core.automation import outbox_metrics, process_due_events

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process due automation outbox messages with retry and dead-letter handling."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of due messages to claim in this invocation.",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        result = process_due_events(limit=options["limit"])
        metrics = outbox_metrics()
        payload = {**result, "metrics": metrics}
        logger.info("automation_outbox_batch", extra=payload)
        rendered = json.dumps(payload, sort_keys=True)
        self.stdout.write(rendered)
        return rendered
