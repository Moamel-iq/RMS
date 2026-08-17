r"""
Populate a development database with the kitchen demo dataset.

Not reference data and not a fixture: five recipes, created through the real
services, so every Task 3.1 screen can be looked at with something on it. See
`docs/development/demo-data-policy.md`.

    .venv\Scripts\python.exe manage.py seed_kitchen_demo --user moamel

There is no `--confirm-demo` flag. That guards the irreversible half of a
seed — posted movements and journals that cannot be deleted — and this seed has
no such half: it creates master data, versions, components and one append-only
cost snapshot, and it moves no stock and writes no journal. The flag arrives
with Task 3.5, when a production batch starts posting. Adding it now would make
it a habit rather than a warning.

The cost snapshot is the one row here that cannot be deleted afterwards, and it
is still not a posting: nothing about it touches a balance or a ledger, and it
carries the demo banner and a reference that says in as many words that it is
not a real decision.

`settings.DEBUG` is checked first, before any argument is read, and no flag
turns it off: demo recipes in production would be indistinguishable from the
branch's real ones on every costing screen the business opens.
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
from apps.kitchen.demo import DEMO_BANNER, seed_demo_recipes
from apps.kitchen.models import RecipeCostSnapshot
from apps.organizations.models import Organization

#: The screens this dataset makes reviewable, in navigation order.
INSPECTION_ROUTES: list[tuple[str, str]] = [
    ("kitchen:recipe_list", "الوصفات"),
    ("kitchen:category_list", "مجموعات الوصفات"),
    ("kitchen:version_list", "نسخ الوصفات"),
    ("kitchen:cost_snapshot_list", "لقطات الكلفة"),
]


class Command(SeedCommand):
    help = (
        "Seed the kitchen demo recipes (DEBUG only). Five recipes against the "
        "existing inventory demo organization, through the real services. "
        "Creates no stock movement and no journal entry."
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
                "seed_kitchen_demo runs only with DEBUG=True. Demo recipes in "
                "production would be indistinguishable from the branch's real ones."
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
                "Run seed_inventory_demo first — recipes build on its items."
            )

        with audit_context(actor=user):
            recipes = seed_demo_recipes(organization=organization, created_by=user)

        self.write("")
        self.write(f"Organization  {organization.code} - {organization.name_ar}")
        self.write(f"Every record below is {DEMO_BANNER}")
        self.write("")
        self.write("recipes:")
        for recipe in recipes:
            state = "archived" if not recipe.is_active else "active"
            versions = ", ".join(
                f"v{version.version_number} {version.status}"
                for version in recipe.versions.order_by("version_number")
            )
            self.write(
                f"  {recipe.code:<22} {recipe.recipe_type:<8} {state:<9} "
                f"{versions or 'no version':<34} {recipe.name_ar}"
            )
        self.write("")
        snapshots = RecipeCostSnapshot.objects.filter(organization=organization)
        if snapshots.exists():
            self.write("")
            self.write("cost snapshots:")
            for snapshot in snapshots.order_by("recipe_code", "as_of_date"):
                self.write(
                    f"  {snapshot.recipe_code:<22} v{snapshot.version_number} "
                    f"@ {snapshot.warehouse_code:<12} {snapshot.as_of_date} "
                    f"total {snapshot.total_material_cost} "
                    f"plate {snapshot.plate_cost} "
                    f"over {snapshot.portions_per_batch} x {snapshot.primary_serving_code} "
                    f"(cutoff {snapshot.ledger_cutoff_sequence})"
                )
                for serving in snapshot.servings.order_by("display_order", "code"):
                    self.write(
                        f"    {serving.code:<8} {serving.whole_serving_count:>7} servings = "
                        f"{serving.normal_serving_count} x {serving.minimum_allocated} + "
                        f"{serving.elevated_serving_count} x {serving.maximum_allocated} + "
                        f"{serving.remainder_cost} leftover = {serving.allocated_total}"
                    )
            self.write(
                "  Append-only: a database trigger refuses UPDATE and DELETE on these "
                "rows for everyone, superusers included."
            )

        self.write("")
        self.write(
            f"{len(recipes)} recipes present. No stock movement, no journal entry, "
            "no production batch."
        )
        self.write(
            "Every approval above is evidenced as DEMO_FICTIONAL, which the database "
            "permits only inside the DEMO- namespace."
        )
        self.write("")
        self.write("Screens to inspect (python manage.py runserver, then):")
        for route, label in INSPECTION_ROUTES:
            self.write(f"  http://127.0.0.1:8000{reverse(route):<28} {label}")
