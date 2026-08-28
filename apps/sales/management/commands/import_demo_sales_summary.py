"""Import a report-level sales summary as an explicitly fictional DEMO day."""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal
from typing import Any

from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.inventory.models import InventoryItem, ItemCategory, ItemType
from apps.inventory.services import create_item, create_item_category
from apps.kitchen.lifecycle import (
    activate_recipe_version,
    approve_recipe_version,
    record_recipe_version_review,
    submit_recipe_version,
)
from apps.kitchen.models import (
    ApprovalEvidenceKind,
    MeasurementBasis,
    Recipe,
    RecipeCategory,
    RecipeReviewDecision,
    RecipeReviewType,
    RecipeType,
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
from apps.sales.day_services import add_sales_line, create_sales_day
from apps.sales.models import (
    DeliveryApplication,
    MenuCategory,
    MenuItem,
    MenuPriceVersion,
    PriceScope,
    SalesChannel,
    SalesDay,
    SalesDayStatus,
)
from apps.sales.services import (
    create_menu_category,
    create_menu_item,
    create_menu_price,
    set_branch_availability,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

DEMO_RECIPE_CODE = "DEMO-SALES-SUMMARY"
DEMO_ITEM_CODE = "DEMO-SALES-MARKER"
DEMO_EVIDENCE = "DEMO-TEST-SALES-20260820"


class Command(SeedCommand):
    help = "Import one report summary into a draft DEMO sales day."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--payload", required=True)
        parser.add_argument("--chef", required=True)
        parser.add_argument("--accountant", required=True)
        parser.add_argument("--manager", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        payload = self._payload(options["payload"])
        organization = self._organization(payload["organization"])
        branch = self._branch(organization, payload["branch"])
        business_date = self._date(payload["business_date"])
        chef = self._user(options["chef"])
        accountant = self._user(options["accountant"])
        manager = self._user(options["manager"])
        if len({chef.pk, accountant.pk, manager.pk}) != 3:
            raise CommandError("Chef, accountant and manager must be distinct users.")

        with transaction.atomic():
            recipe = self._demo_recipe(
                organization=organization,
                branch=branch,
                business_date=business_date,
                chef=chef,
                accountant=accountant,
                manager=manager,
            )
            category = self._menu_category(organization)
            day = SalesDay.objects.filter(branch=branch, business_date=business_date).first()
            if day is None:
                day = create_sales_day(
                    organization=organization,
                    branch=branch,
                    business_date=business_date,
                    actor=manager,
                    notes=self._day_note(payload),
                )
            if day.status != SalesDayStatus.DRAFT:
                raise CommandError("The target sales day is not a draft.")

            existing_codes = set(day.lines.values_list("menu_item__code", flat=True))
            wanted_codes = {str(row["code"]).strip().upper() for row in payload["lines"]}
            if existing_codes:
                if existing_codes == wanted_codes:
                    self.write("سطور DEMO موجودة مسبقاً؛ لم تتكرر.")
                    self._print_totals(day)
                    return
                raise CommandError("The target day already contains other lines.")

            for order, row in enumerate(payload["lines"], start=1):
                item = self._menu_item(
                    organization=organization,
                    branch=branch,
                    recipe=recipe,
                    category=category,
                    row=row,
                    order=order,
                    business_date=business_date,
                )
                channel = SalesChannel.objects.get(
                    organization=organization, code=str(row["channel"]).strip().upper()
                )
                application = None
                if row.get("application"):
                    application = DeliveryApplication.objects.get(
                        organization=organization,
                        code=str(row["application"]).strip().upper(),
                    )
                discount = Decimal(str(row.get("restaurant_discount", "0")))
                add_sales_line(
                    day=day,
                    menu_item=item,
                    channel=channel,
                    quantity=Decimal("1"),
                    delivery_application=application,
                    order_count=int(row.get("order_count", 0)),
                    manual_discount_amount=(discount if discount else None),
                    manual_discount_reason=("خصم تجريبي مطابق لتقرير المصدر." if discount else ""),
                    notes=self._line_note(payload, row),
                )

            self._print_totals(day)
            if options["dry_run"]:
                self.write("dry run — rolled back, nothing was kept.")
                transaction.set_rollback(True)

    def _demo_recipe(
        self,
        *,
        organization: Organization,
        branch: Branch,
        business_date: datetime.date,
        chef: User,
        accountant: User,
        manager: User,
    ) -> Recipe:
        recipe = Recipe.objects.filter(organization=organization, code=DEMO_RECIPE_CODE).first()
        if recipe is None:
            marker = self._marker_item(organization)
            category = RecipeCategory.objects.get(organization=organization, code="PORTION")
            unit = UnitOfMeasure.objects.get(code="PIECE")
            recipe = create_recipe(
                organization=organization,
                code=DEMO_RECIPE_CODE,
                name="تجريبي - ملخص مبيعات التقارير",
                recipe_type=RecipeType.PORTION,
                category=category,
                notes="وصفة تجميع DEMO خيالية؛ لا تمثل صنفاً أو استهلاكاً مخزنياً حقيقياً.",
                created_by=chef,
                source_document="مواد.pdf",
                source_page=1,
                source_reference="تقرير مبيعات المواد",
                source_note="مرجع المبالغ فقط؛ وصفة التجميع خيالية للاختبار.",
            )
            draft = create_draft_recipe_version(
                recipe=recipe,
                expected_output_quantity=Decimal("1"),
                output_unit=unit,
                instructions="تجميع مالي اختباري لملخص التقرير، بلا ادعاء استهلاك مخزني.",
                notes="DEMO فقط؛ لا يُنشر كاستهلاك مخزني.",
                created_by=chef,
                source_document="مواد.pdf",
                source_page=1,
                source_reference="تقرير مبيعات المواد",
                source_note="مرجع مبلغ المبيعات، لا وصفة تشغيل.",
            )
            add_recipe_line(
                version=draft,
                item=marker,
                entered_quantity=Decimal("1"),
                entered_unit=unit,
                measurement_basis=MeasurementBasis.RAW,
                note="علامة DEMO فقط؛ ليست حركة مخزون فعلية.",
                source_document="مواد.pdf",
                source_page=1,
                source_note="سطر تجريبي اصطناعي مطلوب لبنية وصفة DEMO.",
            )
            add_recipe_step(
                version=draft,
                instruction_ar="سجّل إجمالي القناة أو التطبيق كما ورد في تقرير الاختبار.",
                source_document="مواد.pdf",
                source_page=1,
                source_note="خطوة تجميع مالية تجريبية.",
            )
            add_recipe_serving(
                version=draft,
                code="SALE",
                name="سطر مبيعات مجمع",
                serving_quantity=Decimal("1"),
                serving_unit=unit,
                is_primary=True,
                source_document="مواد.pdf",
                source_page=1,
                source_note="حصة DEMO مجمعة وليست طبقاً حقيقياً.",
            )
        version = recipe.versions.order_by("-version_number").first()
        if version is None:
            raise CommandError("The DEMO sales recipe has no version.")
        if version.status == RecipeVersionStatus.DRAFT:
            version = submit_recipe_version(version=version, actor=chef)
        if version.status == RecipeVersionStatus.SUBMITTED:
            for review_type, reviewer in (
                (RecipeReviewType.KITCHEN, chef),
                (RecipeReviewType.STOREKEEPER, chef),
                (RecipeReviewType.ACCOUNTING, accountant),
            ):
                record_recipe_version_review(
                    version=version,
                    review_type=review_type,
                    reviewer=reviewer,
                    decision=RecipeReviewDecision.APPROVED,
                    evidence_reference=DEMO_EVIDENCE,
                    evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
                    note="مراجعة DEMO خيالية لا تمثل توقيعاً حقيقياً.",
                )
            version = approve_recipe_version(
                version=version,
                actor=manager,
                approval_reference=DEMO_EVIDENCE,
                approval_evidence_kind=ApprovalEvidenceKind.DEMO_FICTIONAL,
                note="اعتماد DEMO خيالي للاختبار فقط.",
            )
        if version.status == RecipeVersionStatus.APPROVED:
            activate_recipe_version(
                version=version,
                actor=manager,
                effective_from=business_date,
                branches=[branch],
                reason="تفعيل ملخص مبيعات DEMO للاختبار فقط.",
            )
        return recipe

    def _marker_item(self, organization: Organization) -> InventoryItem:
        item = InventoryItem.objects.filter(organization=organization, code=DEMO_ITEM_CODE).first()
        if item is not None:
            return item
        category = ItemCategory.objects.filter(organization=organization, code="DEMO-SALES").first()
        if category is None:
            category = create_item_category(
                organization=organization,
                code="DEMO-SALES",
                name="علامات اختبار المبيعات",
            )
        return create_item(
            organization=organization,
            code=DEMO_ITEM_CODE,
            name="علامة تجميع مبيعات DEMO",
            category=category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=UnitOfMeasure.objects.get(code="PIECE"),
            notes="صنف خيالي للاختبار؛ لا يمثل مخزوناً حقيقياً.",
        )

    def _menu_category(self, organization: Organization) -> MenuCategory:
        category = MenuCategory.objects.filter(
            organization=organization, code="DEMO-SUMMARY"
        ).first()
        if category is None:
            category = create_menu_category(
                organization=organization,
                code="DEMO-SUMMARY",
                name="ملخصات مبيعات DEMO",
                display_order=999,
            )
        return category

    def _menu_item(
        self,
        *,
        organization: Organization,
        branch: Branch,
        recipe: Recipe,
        category: MenuCategory,
        row: dict[str, Any],
        order: int,
        business_date: datetime.date,
    ) -> MenuItem:
        code = str(row["code"]).strip().upper()
        item = MenuItem.objects.filter(organization=organization, code=code).first()
        if item is None:
            item = create_menu_item(
                organization=organization,
                code=code,
                name=str(row["name"]),
                recipe=recipe,
                serving_code="SALE",
                category=category,
                display_order=900 + order,
                notes="صنف DEMO مجمع من تقرير؛ ليس مادة منيو تشغيلية.",
            )
            set_branch_availability(
                item=item,
                branch=branch,
                notes="متاح فقط لتجربة تقرير المبيعات المجمع.",
            )
        price = MenuPriceVersion.objects.filter(
            menu_item=item,
            branch=branch,
            scope=PriceScope.BRANCH_DEFAULT,
            effective_from=business_date,
            is_active=True,
        ).first()
        amount = Decimal(str(row["amount"]))
        if price is None:
            create_menu_price(
                menu_item=item,
                branch=branch,
                unit_price=amount,
                effective_from=business_date,
                evidence_reference=DEMO_EVIDENCE,
                notes="سعر وحدة واحدة يساوي إجمالي سطر التقرير التجريبي.",
            )
        elif price.unit_price != amount:
            raise CommandError(f"Existing price for {code} differs from payload.")
        return item

    def _print_totals(self, day: SalesDay) -> None:
        lines = day.lines.all()
        gross = sum((line.gross_amount for line in lines), Decimal("0"))
        discount = sum((line.restaurant_discount for line in lines), Decimal("0"))
        customer = sum((line.customer_charge for line in lines), Decimal("0"))
        commission = sum((line.commission_amount for line in lines), Decimal("0"))
        orders = sum(line.order_count for line in lines)
        self.write(f"السطور: {lines.count()}")
        self.write(f"الإجمالي: {gross}")
        self.write(f"خصم المطعم: {discount}")
        self.write(f"تحصيل العميل: {customer}")
        self.write(f"عمولة التطبيقات: {commission}")
        self.write(f"عدد الطلبات الموثق: {orders}")

    @staticmethod
    def _day_note(payload: dict[str, Any]) -> str:
        return (
            "DEMO خيالي للاختبار فقط. "
            f"المصدر: {payload.get('source_document', '')}. "
            f"{payload.get('source_note', '')}"
        ).strip()

    @classmethod
    def _line_note(cls, payload: dict[str, Any], row: dict[str, Any]) -> str:
        return f"{cls._day_note(payload)} {row.get('note', '')}".strip()

    @staticmethod
    def _payload(value: str) -> dict[str, Any]:
        path = pathlib.Path(value)
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload.get("lines"), list) or not payload["lines"]:
            raise CommandError("Payload must contain a non-empty lines list.")
        return payload

    @staticmethod
    def _date(value: str) -> datetime.date:
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as error:
            raise CommandError("business_date must be YYYY-MM-DD.") from error

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
            raise CommandError(f"Branch {code} does not exist.")
        return branch

    @staticmethod
    def _user(username: str) -> User:
        user = User.objects.filter(username=username, is_active=True).first()
        if user is None:
            raise CommandError(f"Active user {username} does not exist.")
        return user
