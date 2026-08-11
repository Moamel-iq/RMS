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
from apps.procurement.demo import seed_demo_suppliers


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

        self.write("")
        self.write(f"Organization  {organization.code} — {organization.name_ar}")
        self.write("suppliers:")
        for supplier in suppliers:
            self.write(f"  {supplier.code:<24} {supplier.name_ar}")
        self.write("")
        self.write(f"{len(suppliers)} suppliers present.")
        self.write("")
        self.write("Screen to inspect (python manage.py runserver, then):")
        self.write(f"  http://127.0.0.1:8000{reverse('procurement:supplier_list'):<34} الموردون")
