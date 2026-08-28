"""Copy real recipe drafts into the explicit DEMO namespace and activate them."""

from __future__ import annotations

import datetime
from typing import Any

from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    record_recipe_version_review,
    submit_recipe_version,
)
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    Recipe,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeVersionStatus,
)
from apps.kitchen.services import (
    add_recipe_line,
    add_recipe_serving,
    add_recipe_step,
    create_draft_recipe_version,
    create_recipe,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User

DEMO_PREFIX = "DEMO-"
DEMO_EVIDENCE = "DEMO-TEST-AUTH-2026-08-20"


class Command(SeedCommand):
    help = "Create and activate explicitly fictional DEMO copies of recipe drafts."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--branch", required=True)
        parser.add_argument("--chef", required=True)
        parser.add_argument("--accountant", required=True)
        parser.add_argument("--manager", required=True)
        parser.add_argument("--effective-from", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        organization = self._organization(options["organization"])
        branch = self._branch(organization, options["branch"])
        chef = self._user(options["chef"])
        accountant = self._user(options["accountant"])
        manager = self._user(options["manager"])
        try:
            effective_from = datetime.date.fromisoformat(options["effective_from"])
        except ValueError as error:
            raise CommandError("--effective-from must be YYYY-MM-DD.") from error

        if len({chef.pk, accountant.pk, manager.pk}) != 3:
            raise CommandError("Chef, accountant and manager must be three distinct users.")

        originals = list(
            Recipe.objects.filter(organization=organization)
            .exclude(code__startswith=DEMO_PREFIX)
            .prefetch_related(
                "branch_applicability__branch",
                "versions__lines__item",
                "versions__lines__entered_unit",
                "versions__lines__package_unit",
                "versions__steps",
                "versions__servings__serving_unit",
            )
            .order_by("code")
        )
        if not originals:
            raise CommandError("No source recipes found.")

        created = activated = 0
        with transaction.atomic():
            for original in originals:
                demo_code = f"{DEMO_PREFIX}{original.code}"
                if len(demo_code) > 32:
                    raise CommandError(f"Demo code is too long: {demo_code}")
                demo = Recipe.objects.filter(organization=organization, code=demo_code).first()
                if demo is None:
                    demo = self._copy_recipe(original=original, code=demo_code, creator=chef)
                    created += 1

                version = demo.versions.order_by("-version_number").first()
                if version is None:
                    raise CommandError(f"{demo.code} has no version.")
                if version.status == RecipeVersionStatus.DRAFT:
                    version = submit_recipe_version(version=version, actor=chef)
                if version.status == RecipeVersionStatus.SUBMITTED:
                    self._record_reviews(version=version, chef=chef, accountant=accountant)
                    version = approve_recipe_version(
                        version=version,
                        actor=manager,
                        approval_reference=DEMO_EVIDENCE,
                        approval_evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
                        note="اعتماد تجريبي خيالي بطلب المالك؛ ليس توقيعاً تشغيلياً حقيقياً.",
                    )
                if version.status == RecipeVersionStatus.APPROVED:
                    activate_recipe_version(
                        version=version,
                        actor=manager,
                        effective_from=effective_from,
                        branches=[branch],
                        reason="تفعيل نسخة DEMO خيالية لاختبار إدخال المبيعات.",
                    )
                    activated += 1

            self.write(f"نسخ DEMO أنشئت: {created}")
            self.write(f"نسخ DEMO فُعّلت: {activated}")
            if options["dry_run"]:
                self.write("dry run — rolled back, nothing was kept.")
                transaction.set_rollback(True)

    def _copy_recipe(self, *, original: Recipe, code: str, creator: User) -> Recipe:
        source = original.versions.filter(status=RecipeVersionStatus.DRAFT).first()
        if source is None:
            raise CommandError(f"{original.code} has no source draft.")
        demo = create_recipe(
            organization=original.organization,
            code=code,
            name=f"تجريبي - {original.name}",
            description_ar=original.description_ar,
            description_en=original.description_en,
            category=original.category,
            recipe_type=original.recipe_type,
            output_item=original.output_item,
            notes=self._demo_note(original.notes),
            created_by=creator,
            source_document=original.source_document,
            source_page=original.source_page,
            source_sha256=original.source_sha256,
            source_reference=original.source_reference,
            source_note=self._demo_note(original.source_note),
        )
        version = create_draft_recipe_version(
            recipe=demo,
            expected_output_quantity=source.expected_output_quantity,
            output_unit=source.output_unit,
            batch_size=source.batch_size,
            preparation_loss=source.preparation_loss,
            cooking_yield=source.cooking_yield,
            instructions=source.instructions,
            notes=self._demo_note(source.notes),
            created_by=creator,
            source_document=source.source_document,
            source_page=source.source_page,
            source_sha256=source.source_sha256,
            source_reference=source.source_reference,
            source_note=self._demo_note(source.source_note),
        )

        for line in source.lines.order_by("line_order"):
            add_recipe_line(
                version=version,
                item=line.item,
                entered_quantity=line.entered_quantity,
                entered_unit=line.entered_unit,
                package_unit=line.package_unit,
                measured_base_quantity=(line.base_quantity if line.package_unit_id else None),
                measured_quantity=line.measured_quantity,
                loss_rate=line.loss_rate,
                cost_class=line.cost_class,
                preparation_stage=line.preparation_stage,
                measurement_basis=line.measurement_basis,
                is_optional=line.is_optional,
                note=line.note,
                line_order=line.line_order,
                source_document=line.source_document,
                source_page=line.source_page,
                source_sha256=line.source_sha256,
                source_reference=line.source_reference,
                source_note=line.source_note,
            )

        for step in source.steps.order_by("sequence"):
            add_recipe_step(
                version=version,
                sequence=step.sequence,
                instruction_ar=step.instruction_ar,
                instruction_en=step.instruction_en,
                stage=step.stage,
                expected_duration=step.expected_duration,
                temperature_c=step.temperature_c,
                heat_instruction_ar=step.heat_instruction_ar,
                checkpoint_ar=step.checkpoint_ar,
                is_critical=step.is_critical,
                media_reference=step.media_reference,
                note=step.note,
                source_document=step.source_document,
                source_page=step.source_page,
                source_sha256=step.source_sha256,
                source_reference=step.source_reference,
                source_note=step.source_note,
            )

        for serving in source.servings.order_by("display_order"):
            add_recipe_serving(
                version=version,
                code=serving.code,
                name=serving.name,
                serving_quantity=serving.serving_quantity,
                serving_unit=serving.serving_unit,
                is_primary=serving.is_primary,
                rounding_increment=serving.rounding_increment,
                rounding_policy=serving.rounding_policy,
                measurement_basis=serving.measurement_basis,
                display_order=serving.display_order,
                source_document=serving.source_document,
                source_page=serving.source_page,
                source_sha256=serving.source_sha256,
                source_reference=serving.source_reference,
                source_note=serving.source_note,
            )
        return demo

    def _record_reviews(self, *, version: Any, chef: User, accountant: User) -> None:
        recorded = set(version.reviews.values_list("review_type", flat=True))
        for review_type, reviewer, note in (
            (
                RecipeReviewType.KITCHEN,
                chef,
                "مراجعة مطبخ تجريبية خيالية؛ لا تمثل توقيع الموظف الحقيقي.",
            ),
            (
                RecipeReviewType.STOREKEEPER,
                chef,
                "مراجعة مخزن تجريبية خيالية؛ لا تمثل توقيع الموظف الحقيقي.",
            ),
            (
                RecipeReviewType.ACCOUNTING,
                accountant,
                "مراجعة كلفة تجريبية خيالية؛ لا تمثل توقيع الموظف الحقيقي.",
            ),
        ):
            if review_type in recorded:
                continue
            record_recipe_version_review(
                version=version,
                review_type=review_type,
                reviewer=reviewer,
                decision=RecipeReviewDecision.APPROVED,
                evidence_reference=DEMO_EVIDENCE,
                evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
                note=note,
            )

    @staticmethod
    def _demo_note(value: str) -> str:
        prefix = "بيانات DEMO خيالية للاختبار فقط؛ غير صالحة للتشغيل الحقيقي."
        return f"{prefix} {value}".strip()

    @staticmethod
    def _organization(code: str) -> Organization:
        organization = Organization.objects.filter(code=code).first()
        if organization is None:
            raise CommandError(f"Organization {code} does not exist.")
        return organization

    @staticmethod
    def _branch(organization: Organization, code: str) -> Branch:
        branch = Branch.objects.filter(organization=organization, code=code).first()
        if branch is None:
            raise CommandError(f"Branch {code} does not exist in {organization.code}.")
        return branch

    @staticmethod
    def _user(username: str) -> User:
        user = User.objects.filter(username=username, is_active=True).first()
        if user is None:
            raise CommandError(f"Active user {username} does not exist.")
        return user
