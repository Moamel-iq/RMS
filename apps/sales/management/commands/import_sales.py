r"""
Load a day's sales from a CSV the branch can maintain by hand.

    .venv\Scripts\python.exe manage.py import_sales ^
        --organization 01 --branch 011 --actor <username> ^
        --file "<a path outside this repository>\sales.csv"

The file has one row per dish per channel per day:

    التاريخ · اسم الطبق · القناة · الكمية · خصم المطعم · سبب الخصم · ملاحظات

A dish is named the way the menu names it, not by a code, because the person
typing this reads a menu and not a database. The importer resolves the name
against the menu items already loaded, and a name it cannot find is reported
with the row it came from rather than guessed at — two dishes differing by one
word is exactly the case where a fuzzy match would post the wrong revenue.

There is no price column, and that is the system's design rather than an
omission: the price comes from the menu version effective on that business
date, so a day cannot be entered at a price the menu never carried. Selling
below it is a **discount**, which has its own column and needs a reason.

**Days land as DRAFT.** Posting a sales day writes revenue to the ledger and
consumes stock, and that is a cashier's close and a manager's signature, not an
importer's. The file is data entry; the posting is a decision.
"""

from __future__ import annotations

import csv
import datetime
import pathlib
from collections import defaultdict
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.organizations.models import Branch, Organization
from apps.sales.models import MenuItem, SalesChannel
from apps.users.models import User

#: The channel names the sheet uses, mapped to the codes the system holds.
CHANNELS = {
    "الصالة": "HALL",
    "سفري": "TAKEAWAY",
    "دليفري": "DELIVERY",
    "التطبيقات": "APPLICATIONS",
}


class Command(SeedCommand):
    help = "Import daily sales from a CSV kept outside this repository."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--branch", required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization {options['organization']}.")
        branch = Branch.objects.filter(organization=organization, code=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch {options['branch']}.")
        actor = User.objects.filter(username=options["actor"]).first()
        if actor is None:
            raise CommandError(f"No user {options['actor']}.")

        path = pathlib.Path(options["file"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")

        menu = {
            item.name_ar.strip(): item
            for item in MenuItem.objects.filter(organization=organization, is_active=True)
        }
        channels = {
            channel.code: channel
            for channel in SalesChannel.objects.filter(organization=organization)
        }

        by_day: dict[datetime.date, list[dict[str, Any]]] = defaultdict(list)
        problems: list[str] = []

        with path.open(encoding="utf-8-sig", newline="") as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                name = (row.get("اسم الطبق") or "").strip()
                if not name:
                    continue
                note = (row.get("ملاحظات") or "").strip()
                if "مثال" in note:
                    # The template ships with sample rows; they are meant to be
                    # deleted and are skipped rather than posted as revenue.
                    continue

                raw_date = (row.get("التاريخ") or "").strip()
                try:
                    day = datetime.date.fromisoformat(raw_date)
                except ValueError:
                    problems.append(f"سطر {number}: تاريخ غير صالح «{raw_date}»")
                    continue

                item = menu.get(name)
                if item is None:
                    problems.append(f"سطر {number}: لا صنف منيو باسم «{name}»")
                    continue

                label = (row.get("القناة") or "").strip()
                code = CHANNELS.get(label)
                if code is None or code not in channels:
                    problems.append(f"سطر {number}: قناة غير معروفة «{label}»")
                    continue

                try:
                    quantity = Decimal((row.get("الكمية") or "0").strip() or "0")
                except ArithmeticError, ValueError:
                    problems.append(f"سطر {number}: كمية غير صالحة")
                    continue
                if quantity <= 0:
                    continue

                by_day[day].append(
                    {
                        "menu_item": item,
                        "channel": channels[code],
                        "quantity": quantity,
                        "discount": (row.get("خصم المطعم") or "0").strip() or "0",
                        "reason": (row.get("سبب الخصم") or "").strip(),
                        "row": number,
                    }
                )

        made_days = made_lines = 0
        with transaction.atomic():
            for day in sorted(by_day):
                try:
                    sales_day = self._day(organization, branch, day, actor)
                except ValidationError as refused:
                    problems.append(f"{day}: {'; '.join(refused.messages)}")
                    continue
                made_days += 1
                for line in by_day[day]:
                    try:
                        self._line(sales_day, line, actor)
                        made_lines += 1
                    except ValidationError as refused:
                        problems.append(
                            f"سطر {line['row']} ({line['menu_item'].name_ar}): "
                            f"{'; '.join(refused.messages)}"
                        )

            self.write("")
            self.write(f"=== مبيعات · {organization.code} · {branch.code} ===")
            self.write(f"  أيام أُنشئت : {made_days}")
            self.write(f"  سطور        : {made_lines}")
            self.write(f"  مشاكل       : {len(problems)}")
            for problem in problems[:20]:
                self.write(f"    · {problem}")
            self.write("")
            self.write("  الأيام مسوّدات. الترحيل يكتب الإيراد في الدفتر ويستهلك المخزون،")
            self.write("  وهو إقفال الكاشير وتوقيع المدير لا توقيع المستورِد.")

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back.")
                transaction.set_rollback(True)

    def _day(
        self, organization: Organization, branch: Branch, day: datetime.date, actor: User
    ) -> Any:
        from apps.sales.day_services import create_sales_day
        from apps.sales.models import SalesDay

        existing = SalesDay.objects.filter(
            organization=organization, branch=branch, business_date=day
        ).first()
        if existing is not None:
            return existing
        return create_sales_day(
            organization=organization, branch=branch, business_date=day, actor=actor
        )

    def _line(self, sales_day: Any, line: dict[str, Any], actor: User) -> Any:
        from apps.sales.day_services import add_sales_line

        kwargs: dict[str, Any] = {
            "day": sales_day,
            "menu_item": line["menu_item"],
            "channel": line["channel"],
            "quantity": line["quantity"],
        }
        discount = Decimal(line["discount"] or "0")
        if discount > 0:
            # A discount without a stated reason is a price change nobody
            # signed for, and the service refuses one — so the sheet's reason
            # column is passed through rather than defaulted to a blank.
            kwargs["manual_discount_amount"] = discount
            kwargs["manual_discount_reason"] = line["reason"]
        return add_sales_line(**kwargs)
