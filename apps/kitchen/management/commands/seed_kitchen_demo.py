r"""
Populate a development database with the kitchen demo dataset.

Not reference data and not a fixture: five recipes, created through the real
services, so every Task 3.1 screen can be looked at with something on it. See
`docs/development/demo-data-policy.md`.

    .venv\Scripts\python.exe manage.py seed_kitchen_demo --user moamel

There is no `--confirm-demo` flag. That guards the irreversible half of a
seed — posted movements and journals that cannot be deleted — and Task 3.1 has
no such half: it creates master data and drafts, moves no stock and writes no
journal. The flag arrives with Task 3.5, when a production batch starts
posting. Adding it now would make it a habit rather than a warning.

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
from apps.organizations.models import Organization

#: The screens this dataset makes reviewable, in navigation order.
INSPECTION_ROUTES: list[tuple[str, str]] = [
    ("kitchen:recipe_list", "الوصفات"),
    ("kitchen:category_list", "مجموعات الوصفات"),
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
            draft = recipe.versions.first()
            state = "archived" if not recipe.is_active else "active"
            version = f"v{draft.version_number} DRAFT" if draft else "no draft"
            self.write(
                f"  {recipe.code:<20} {recipe.recipe_type:<8} {state:<9} {version:<12} "
                f"{recipe.name_ar}"
            )
        self.write("")
        self.write(
            f"{len(recipes)} recipes present. No stock movement, no journal entry, "
            "no approved version."
        )
        self.write("")
        self.write("Screens to inspect (python manage.py runserver, then):")
        for route, label in INSPECTION_ROUTES:
            self.write(f"  http://127.0.0.1:8000{reverse(route):<28} {label}")
