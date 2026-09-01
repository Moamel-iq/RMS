"""
Make the insights screens show something, without inventing a business fact.

A screen that has never rendered a row has not been reviewed, and this one is
harder than most to review: it only says anything when the *absence* of stock
issues meets the *presence* of theoretical consumption, which needs recipes,
sales and receipts all lined up.

## What it does, and the line it does not cross

It runs the real orchestration service over `DEMO-INSIGHTS-V1`, against
whatever the demo inventory, kitchen and sales seeds already posted. It creates
no movement, no recipe, no sale, and above all **no finding**: findings come
out of the detector reading posted data, or they do not come out at all. A
command that wrote an `Insight` directly would be showing the reviewer a
screenshot, not a system.

So if this produces nothing, that is the honest answer and it is printed as
one: it means the demo ledger has no item that was received, is implied by a
recipe, and was never issued. Run `seed_inventory_demo`, `seed_kitchen_demo`
and `seed_sales_demo` first.

Per `docs/development/demo-data-policy.md`: `DEBUG`-only, idempotent (a second
run reuses the same cases and appends one observation, exactly as a second
scheduled run would), namespaced, and never automatic.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.insights.models import Insight, RunTrigger
from apps.insights.services import run_insights
from apps.organizations.models import Organization
from apps.users.models import User

NAMESPACE = "DEMO-INSIGHTS-V1"


class Command(SeedCommand):
    help = "Run the insights detectors over the demo ledger so the screens render."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", default="DEMO", help="Organization code.")
        parser.add_argument("--window", type=int, default=120, help="Business days to analyse.")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            raise ValidationError("بيانات العرض التوضيحي لا تعمل خارج وضع التطوير.")

        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            available = ", ".join(Organization.objects.values_list("code", flat=True)) or "—"
            raise ValidationError(
                f"لا توجد مؤسسة بالرمز {options['organization']!r}. المتاح: {available}"
            )

        actor = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
        if actor is None:
            raise ValidationError("لا يوجد حساب مسؤول لتشغيل التحليل التوضيحي.")

        # A wide window on purpose: the demo ledger is a handful of dated
        # documents rather than a continuous month, and a 28-day window over
        # it would usually see nothing at all.
        end = datetime.date.today()
        start = end - datetime.timedelta(days=int(options["window"]))
        before = Insight.objects.filter(organization=organization).count()

        self.write(f"{NAMESPACE}: تحليل {organization.code} من {start} إلى {end}")
        run = run_insights(
            organization=organization,
            actor=actor,
            period_start=start,
            period_end=end,
            trigger=RunTrigger.MANUAL,
        )

        after = Insight.objects.filter(organization=organization).count()
        for outcome in run.outcomes.all():
            self.write(
                f"  {outcome.detector_code}: {outcome.outcome} · "
                f"تغطية {outcome.coverage} · مرشحون {outcome.candidate_count}"
            )
            for key, value in (outcome.notes or {}).items():
                if key != "identity_failures_sample":
                    self.write(f"      {key}: {value}")

        self.write(f"حالات جديدة: {after - before} · إجمالي الحالات: {after}")
        if after == 0:
            # Said plainly rather than left as an empty screen: the reviewer
            # needs to know this is missing prerequisites, not a broken page.
            self.write(
                "لم تُنتج بيانات العرض أي ملاحظة. الكاشف يحتاج صنفاً استُلم، "
                "وتشير الوصفات والمبيعات إلى استهلاكه، ولم تُسجَّل له صرفيات. "
                "شغّل seed_inventory_demo و seed_kitchen_demo و seed_sales_demo أولاً."
            )
