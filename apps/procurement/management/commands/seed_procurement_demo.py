"""
Populate a development database with the procurement demo dataset.

Not reference data and not a fixture: three suppliers, created through the real
services, so the supplier screen can be looked at with something on it. See
`docs/development/demo-data-policy.md`.

    .venv\\Scripts\\python.exe manage.py seed_procurement_demo --user moamel

Task 2.1 creates master data only, so there is no `--confirm-demo` here. That
flag guards the irreversible half of a seed — posted movements and journals
that cannot be deleted — and a supplier has no such half yet. It arrives with
Task 2.8, when a goods receipt starts posting to the ledger. Adding it now
would make it a habit rather than a warning.

`settings.DEBUG` is checked first, before any argument is read, and no flag
turns it off: demo suppliers in production would be indistinguishable from real
ones on every purchase order the business raises.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from django.urls import reverse

from apps.core.console import SeedCommand
from apps.core.context import audit_context
from apps.inventory.demo import DEMO_ORGANIZATION_CODE, DemoSelectionError
from apps.inventory.management.commands.seed_inventory_demo import resolve_user
from apps.organizations.models import Organization
from apps.procurement.demo import (
    seed_demo_catalogue,
    seed_demo_quotations,
    seed_demo_requests,
    seed_demo_suppliers,
)
from apps.users.models import User

#: The demo storekeeper the inventory seed creates. Requests are raised by
#: them and decided by the signed-in user, so the two actors differ and the
#: maker-checker constraint is satisfied by real people rather than a
#: workaround.
CHECKER_USERNAME = "demo-storekeeper"


#: The screens this dataset makes reviewable, in navigation order. Route
#: names rather than literal paths: a hard-coded path is a second copy of
#: the URL configuration, and the copy is the one that goes stale.
INSPECTION_ROUTES: list[tuple[str, str]] = [
    ("procurement:supplier_list", "الموردون"),
    ("procurement:supplier_item_list", "كتالوج الموردين"),
    ("procurement:purchase_request_list", "طلبات الشراء"),
    ("procurement:quotation_list", "عروض الموردين"),
]


class Command(SeedCommand):
    help = (
        "Seed the procurement demo suppliers (DEBUG only). Three suppliers "
        "against the existing inventory demo organization, through the real services."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--user",
            required=True,
            help="Username, email, or id of the person the audit trail will name.",
        )
        parser.add_argument(
            "--organization",
            default=DEMO_ORGANIZATION_CODE,
            help=f"Organization code. Default {DEMO_ORGANIZATION_CODE}.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            raise CommandError(
                "seed_procurement_demo runs only with DEBUG=True. Demo suppliers "
                "in production would be indistinguishable from real ones."
            )

        try:
            user = resolve_user(options["user"])
        except DemoSelectionError as problem:
            raise CommandError(str(problem)) from problem

        code = options["organization"].strip().upper()
        organization = Organization.objects.filter(code=code).first()
        if organization is None:
            known = ", ".join(Organization.objects.order_by("code").values_list("code", flat=True))
            raise CommandError(
                f"No organization {code!r}. Known: {known or 'none'}. "
                "Run seed_inventory_demo first — procurement builds on its items."
            )

        with audit_context(actor=user):
            suppliers = seed_demo_suppliers(organization=organization)
            catalogue = seed_demo_catalogue(organization=organization)
            # Two actors, because maker-checker is a database constraint
            # and a demo that approved its own request would not insert.
            approver = User.objects.filter(username=CHECKER_USERNAME).first()
            requests = (
                seed_demo_requests(organization=organization, requester=approver, approver=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            quotations = seed_demo_quotations(organization=organization, recorder=user)

        self.write("")
        self.write(f"Organization  {organization.code} — {organization.name_ar}")
        self.write("suppliers:")
        for supplier in suppliers:
            self.write(f"  {supplier.code:<24} {supplier.name_ar}")
        self.write("")
        self.write("catalogue:")
        for row in catalogue:
            package = row.package_unit.code if row.package_unit else row.item.base_unit.code
            self.write(f"  {row.supplier.code:<24} {row.item.code:<16} {package}")
        self.write("")
        if requests:
            self.write("")
            self.write("purchase requests:")
            for document in requests:
                label = document.number or "draft"
                self.write(f"  {label:<24} {document.get_status_display()}")
        self.write("")
        if quotations:
            self.write("")
            self.write("quotations:")
            for quotation in quotations:
                self.write(
                    f"  {quotation.number:<24} {quotation.supplier.code:<24} "
                    f"{quotation.total_amount}"
                )
        self.write("")
        self.write(
            f"{len(suppliers)} suppliers, {len(catalogue)} catalogue rows, "
            f"{len(requests)} requests and {len(quotations)} quotations present."
        )
        self.write("")
        self.write("Screens to inspect (python manage.py runserver, then):")
        for route, label in INSPECTION_ROUTES:
            self.write(f"  http://127.0.0.1:8000{reverse(route):<34} {label}")
