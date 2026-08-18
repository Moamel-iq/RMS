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
from apps.kitchen.models import (
    BatchDocumentLink,
    MealRecord,
    MealRecordStatus,
    ProductionBatch,
    RecipeCostSnapshot,
)
from apps.organizations.models import Organization

#: The screens this dataset makes reviewable, in navigation order.
#: Every Kitchen screen the demo actually populates. Kept in step with
#: `apps/core/navigation.py`: an entry here that 404s or renders empty is the
#: demo telling an operator to look at nothing.
INSPECTION_ROUTES: list[tuple[str, str]] = [
    ("kitchen:recipe_list", "الوصفات"),
    ("kitchen:category_list", "مجموعات الوصفات"),
    ("kitchen:version_list", "نسخ الوصفات"),
    ("kitchen:cost_snapshot_list", "كلفة الوصفة والطبق"),
    ("kitchen:production_list", "أوامر الإنتاج"),
    ("kitchen:report_productivity", "الإنتاجية والفاقد"),
    ("kitchen:report_kitchen_issue", "الصرف للمطبخ"),
    ("kitchen:report_kitchen_return", "المرتجع من المطبخ"),
    ("kitchen:report_kitchen_waste", "الهالك"),
    ("kitchen:meal_staff_list", "وجبات الموظفين"),
    ("kitchen:meal_complimentary_list", "الوجبات المجانية"),
    ("kitchen:report_warehouse_flow", "تدفق مخزن المطبخ"),
    ("kitchen:report_actual_consumption", "الاستهلاك الفعلي"),
    ("kitchen:report_theoretical_consumption", "الاستهلاك النظري"),
    ("kitchen:report_usage_variance", "انحراف الاستهلاك"),
    ("kitchen:report_production_standard", "متطلبات الإنتاج القياسية"),
]


class Command(SeedCommand):
    help = (
        "Seed the kitchen demo recipes (DEBUG only). Five recipes against the "
        "existing inventory demo organization, through the real services. "
        "Posts production, records meals, and attributes two inventory documents "
        "to a batch — all through domain services, never by direct insert."
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

        batches = ProductionBatch.objects.filter(organization=organization)
        if batches.exists():
            self.write("")
            self.write("production drafts:")
            for batch in batches.select_related("recipe", "warehouse").order_by("pk"):
                self.write(
                    f"  {batch.recipe.code:<22} v{batch.recipe_version.version_number} "
                    f"@ {batch.warehouse.code:<12} {batch.planned_business_date} "
                    f"x{batch.multiplier_display} "
                    f"expected {batch.expected_output_display} "
                    f"actual {batch.actual_output_display or '—'} "
                    f"[{batch.status}]"
                )
                for line in batch.lines.order_by("line_order"):
                    path = line.component_path or "direct"
                    rows = " + ".join(
                        f"{row.item.code}={row.quantity_display}"
                        for row in line.actuals.order_by("entry_order")
                    )
                    self.write(
                        f"    {line.line_order:>2} {path:<8} {line.item_code:<18} "
                        f"plan {line.planned_display:<14} actual {rows or 'none'}"
                    )
            self.write(
                "  Statuses above are real: Task 3.5 removed "
                "production_batch_is_draft_only_until_task_3_5 in migration 0017, and a "
                "POSTED batch here has moved stock through post_production_batch."
            )

        self.write("")
        self.write(
            f"{len(recipes)} recipes present, with posted production, staff and "
            "complimentary meals, and Task 3.8 consumption attribution."
        )
        self.write(
            "Every movement and journal above was produced by a domain service. "
            "Nothing in this command writes a movement, a balance or a journal directly."
        )
        self.write(
            "Every approval above is evidenced as DEMO_FICTIONAL, which the database "
            "permits only inside the DEMO- namespace."
        )
        links = BatchDocumentLink.objects.filter(organization=organization)
        meals = MealRecord.objects.filter(organization=organization)
        self.write("")
        self.write(
            f"meal records: {meals.count()} "
            f"({meals.filter(status=MealRecordStatus.CANCELLED).count()} cancelled and "
            "excluded from theoretical consumption)"
        )
        self.write(
            f"batch document links: {links.count()} — explanatory only. They move no "
            "stock, write no journal, and change no batch's consumption or value."
        )
        self.write(
            "Sales-based theoretical consumption is unavailable: the SALES adapter is "
            "absent until Phase 4, and every theoretical and variance surface reports "
            "SALES_NOT_INCLUDED_PHASE_4."
        )
        self.write("")
        self.write("Screens to inspect (python manage.py runserver, then):")
        for route, label in INSPECTION_ROUTES:
            self.write(f"  http://127.0.0.1:8000{reverse(route):<46} {label}")
