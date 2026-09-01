"""
Run the analysis once, from a scheduler or a terminal.

Both paths call `run_insights` — the same service, the same lock, the same
window rules. Two entry points into analytics would eventually mean two
definitions of "the last 28 days", and the day they disagree is the day two
findings about one week cite different numbers.

Deliberately **not** an HTTP action. The scan reads every posted movement in
the window across every kitchen warehouse; doing that inside a request would
hold a worker open for as long as the data takes, and the first busy month
would turn the button into a timeout. Render's scheduled jobs call this.

`SeedCommand` is the base for the Arabic output alone: this writes no
reference data, but it needs `write()`, whose whole purpose is that a Windows
console which cannot render an Arabic item name costs a log line rather than
the transaction.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandParser
from django.utils import timezone

from apps.core.console import SeedCommand
from apps.insights.detectors import base as registry
from apps.insights.models import RunTrigger
from apps.insights.services import DEFAULT_WINDOW_DAYS, run_insights
from apps.organizations.models import Branch, Organization
from apps.users.models import User

_WINDOW = re.compile(r"^(\d+)d$")


class Command(SeedCommand):
    help = "Run the Jadwa Insights detectors for one organization."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True, help="Organization code.")
        parser.add_argument("--branch", default="", help="Optional branch code.")
        parser.add_argument(
            "--window",
            default=f"{DEFAULT_WINDOW_DAYS}d",
            help="Completed business days to analyse, e.g. 28d.",
        )
        parser.add_argument(
            "--detector",
            action="append",
            default=[],
            dest="detectors",
            help="Run only this detector. Repeatable.",
        )
        parser.add_argument(
            "--actor",
            default="",
            help="Username to attribute a manual run to. Omit for a scheduled run.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            available = ", ".join(Organization.objects.values_list("code", flat=True)) or "—"
            raise ValidationError(
                f"لا توجد مؤسسة بالرمز {options['organization']!r}. المتاح: {available}"
            )

        branch = None
        if options["branch"]:
            branch = Branch.objects.filter(
                organization=organization, code=options["branch"]
            ).first()
            if branch is None:
                raise ValidationError(f"لا يوجد فرع بالرمز {options['branch']!r} في هذه المؤسسة.")

        match = _WINDOW.match(str(options["window"]).strip())
        if not match or int(match.group(1)) < 1:
            raise ValidationError("صيغة النافذة غير صالحة. مثال صحيح: 28d")
        days = int(match.group(1))

        detectors = list(options["detectors"]) or None
        if detectors:
            unknown = sorted(set(detectors) - set(registry.known_codes()))
            if unknown:
                raise ValidationError(
                    f"كاشف غير معروف: {'، '.join(unknown)}. "
                    f"المتاح: {'، '.join(registry.known_codes())}"
                )

        actor = None
        trigger = RunTrigger.SCHEDULED
        if options["actor"]:
            actor = User.objects.filter(username=options["actor"]).first()
            if actor is None:
                raise ValidationError(f"لا يوجد مستخدم باسم {options['actor']!r}.")
            trigger = RunTrigger.MANUAL
        else:
            # The engines this reads through are permission-scoped and take a
            # user. A scheduled run has no person behind it, so it borrows a
            # superuser to read with — and reads only; nothing it can do
            # depends on who that is.
            actor = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
            if actor is None:
                raise ValidationError("لا يوجد حساب مسؤول فعّال لتشغيل التحليل المجدول.")

        end = timezone.localdate()
        start = end - datetime.timedelta(days=days)

        self.write(f"المؤسسة: {organization.code} — {organization.name}")
        if branch is not None:
            self.write(f"الفرع: {branch.code} — {branch.name}")
        self.write(f"الفترة: {start} → {end} (نهاية غير شاملة)")

        run = run_insights(
            organization=organization,
            actor=actor,
            branch=branch,
            period_start=start,
            period_end=end,
            detector_codes=detectors,
            trigger=trigger,
        )

        failures = 0
        for outcome in run.outcomes.all():
            line = (
                f"  {outcome.detector_code}: {outcome.outcome} · "
                f"تغطية {outcome.coverage} · ملاحظات {outcome.candidate_count}"
            )
            if outcome.error_summary:
                line += f" · {outcome.error_summary}"
            self.write(line)
            if outcome.outcome == "FAILED":
                failures += 1

        self.write(f"انتهى التحليل. المعرّف: {run.public_id}")
        if failures:
            # A nonzero exit so a scheduler notices. The run and every
            # successful sibling outcome are already recorded — a failure
            # reports itself, it does not erase what worked.
            raise ValidationError(f"أخفق {failures} كاشف. راجع السجل الفني.")
