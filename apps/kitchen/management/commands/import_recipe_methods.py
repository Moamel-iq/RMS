"""Complete open recipe drafts with source-backed preparation methods."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.kitchen.models import Recipe, RecipeType, RecipeVersionStatus
from apps.kitchen.services import (
    add_recipe_serving,
    add_recipe_step,
    update_draft_recipe_version,
)
from apps.organizations.models import Organization


class Command(SeedCommand):
    help = "Import source-backed instructions and steps into existing recipe drafts."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True, help="Organization code.")
        parser.add_argument("--payload", required=True, help="UTF-8 JSON method payload.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report the import, then roll the transaction back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization with code {options['organization']}.")

        path = pathlib.Path(options["payload"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CommandError(f"Cannot read {path}: {error}") from error

        entries = payload.get("recipes")
        if not isinstance(entries, list):
            raise CommandError("Payload must contain a recipes list.")

        completed = skipped = step_count = serving_count = 0
        with transaction.atomic():
            for entry in entries:
                code = str(entry.get("code", "")).strip()
                if not code:
                    raise CommandError("Every method entry must have a recipe code.")
                recipe = Recipe.objects.filter(organization=organization, code=code).first()
                if recipe is None:
                    raise CommandError(f"Recipe {code} does not exist.")
                version = recipe.versions.filter(status=RecipeVersionStatus.DRAFT).first()
                if version is None:
                    raise CommandError(f"Recipe {code} has no open draft.")

                instructions = str(entry.get("instructions", "")).strip()
                steps = entry.get("steps")
                if not instructions or not isinstance(steps, list) or not steps:
                    raise CommandError(f"Recipe {code} must have instructions and steps.")

                if recipe.recipe_type == RecipeType.BATCH and not version.servings.exists():
                    add_recipe_serving(
                        version=version,
                        code="BATCH",
                        name="دفعة الإنتاج الكاملة",
                        serving_quantity=version.expected_output_quantity,
                        serving_unit=version.output_unit,
                        is_primary=True,
                        source_document=version.source_document,
                        source_page=version.source_page,
                        source_sha256=version.source_sha256,
                        source_reference=version.source_reference,
                        source_note=(
                            "الحصة هي كامل دفعة الإنتاج؛ لا تدّعي وزناً أو عدداً "
                            "جزئياً غير مذكور في المصدر."
                        ),
                    )
                    serving_count += 1

                if version.instructions.strip() and version.steps.exists():
                    skipped += 1
                    continue

                if not version.instructions.strip():
                    version = update_draft_recipe_version(
                        version=version,
                        expected_output_quantity=version.expected_output_quantity,
                        output_unit=version.output_unit,
                        batch_size=version.batch_size,
                        preparation_loss=version.preparation_loss,
                        cooking_yield=version.cooking_yield,
                        instructions=instructions,
                        notes=version.notes,
                    )

                if not version.steps.exists():
                    for sequence, step in enumerate(steps, start=1):
                        text = str(step.get("instruction_ar", "")).strip()
                        document = str(step.get("source_document", "")).strip()
                        page = step.get("source_page")
                        if not text or not document or not isinstance(page, int) or page <= 0:
                            raise CommandError(
                                f"Recipe {code}, step {sequence} needs text, document and page."
                            )
                        add_recipe_step(
                            version=version,
                            sequence=sequence,
                            instruction_ar=text,
                            source_document=document,
                            source_page=page,
                            source_sha256=str(step.get("source_sha256", "")).strip(),
                            source_reference=f"صفحة {page}",
                            source_note=str(step.get("source_note", "")).strip(),
                        )
                        step_count += 1

                completed += 1

            self.write(f"وصفات اكتملت طرقها: {completed}")
            self.write(f"وصفات موجودة مسبقاً: {skipped}")
            self.write(f"خطوات أضيفت: {step_count}")
            self.write(f"حصص دفعة كاملة أضيفت: {serving_count}")
            if options["dry_run"]:
                self.write("dry run — rolled back, nothing was kept.")
                transaction.set_rollback(True)
