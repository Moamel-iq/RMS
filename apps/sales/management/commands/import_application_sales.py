from __future__ import annotations

import datetime
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.accounting.models import (
    DELIVERY_APP_RECEIVABLE,
    DELIVERY_COMMISSION_EXPENSE,
    DELIVERY_OTHER_FEE_EXPENSE,
    DELIVERY_SETTLEMENT_VARIANCE,
    SALES_CARD_CLEARING,
    SALES_CASH_ON_HAND,
    SALES_CASH_OVER_SHORT,
    SALES_DISCOUNT,
    SALES_RETURNS,
    SALES_REVENUE,
    SALES_SETTLEMENT_BANK,
    Account,
    AccountRole,
    CostCenter,
    OrganizationAccountMapping,
)
from apps.accounting.services import create_account_mapping
from apps.organizations.models import Branch, Organization
from apps.sales.models import (
    CommissionBasis,
    DeliveryAgreement,
    DeliveryApplication,
    DeliveryApplicationBranchSetting,
    MenuItem,
    MenuPriceVersion,
    PriceScope,
    SalesChannel,
    SalesChannelCategory,
)
from apps.sales.services import (
    archive_menu_price,
    close_delivery_agreement,
    close_menu_price,
    create_delivery_agreement,
    create_delivery_application,
    create_menu_price,
    create_sales_channel,
    set_application_branch_setting,
)

STANDARD_SALES_ACCOUNT_MAPPINGS: tuple[tuple[str, str], ...] = (
    (SALES_REVENUE, "4-01-01-001"),
    (SALES_DISCOUNT, "4-02-01-001"),
    (SALES_RETURNS, "4-03-01-001"),
    (SALES_CASH_ON_HAND, "1-01-01-001"),
    (SALES_CARD_CLEARING, "1-01-03-001"),
    (DELIVERY_APP_RECEIVABLE, "1-02-01-009"),
    (DELIVERY_COMMISSION_EXPENSE, "6-03-01-001"),
    (DELIVERY_OTHER_FEE_EXPENSE, "6-03-01-002"),
    (DELIVERY_SETTLEMENT_VARIANCE, "7-09-05-001"),
    (SALES_SETTLEMENT_BANK, "1-01-02-001"),
    (SALES_CASH_OVER_SHORT, "7-09-06-001"),
)


class Command(BaseCommand):
    help = (
        "Import real delivery applications, branch agreements, and only the menu "
        "prices that differ from the branch default."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--branch", required=True)
        parser.add_argument("--file", required=True, type=Path)
        parser.add_argument("--effective-from", required=True, help="YYYY-MM-DD")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist the import. Without this flag the complete run is rolled back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        payload = self._payload(options["file"])
        try:
            effective_from = datetime.date.fromisoformat(options["effective_from"])
            organization = Organization.objects.get(code=options["organization"])
            branch = Branch.objects.get(organization=organization, code=options["branch"])
        except ValueError as exc:
            raise CommandError("--effective-from must use YYYY-MM-DD.") from exc
        except (Organization.DoesNotExist, Branch.DoesNotExist) as exc:
            raise CommandError(f"Required organization setup is missing: {exc}") from exc

        stats = {
            "account_mappings_created": 0,
            "agreements_created": 0,
            "agreements_reused": 0,
            "applications_created": 0,
            "applications_reused": 0,
            "branch_settings_created": 0,
            "branch_settings_reused": 0,
            "channel_created": 0,
            "prices_archived": 0,
            "prices_closed": 0,
            "prices_created": 0,
            "prices_reused": 0,
        }

        with transaction.atomic():
            self._ensure_account_mappings(
                organization=organization,
                effective_from=datetime.date(effective_from.year, 1, 1),
                stats=stats,
            )
            channel = self._channel(
                organization=organization,
                payload=payload,
                stats=stats,
            )
            self._applications(
                organization=organization,
                branch=branch,
                payload=payload,
                effective_from=effective_from,
                stats=stats,
            )
            self._prices(
                organization=organization,
                branch=branch,
                channel=channel,
                payload=payload,
                effective_from=effective_from,
                stats=stats,
            )
            if not options["commit"]:
                transaction.set_rollback(True)

        mode = "COMMITTED" if options["commit"] else "DRY RUN (rolled back)"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {json.dumps(stats, sort_keys=True)}"))

    def _payload(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Could not read {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CommandError("The source file must contain a JSON object.")
        if not payload.get("evidence_reference"):
            raise CommandError("The source file must state evidence_reference.")
        if not isinstance(payload.get("applications"), list) or not payload["applications"]:
            raise CommandError("The source file must contain applications.")
        if not isinstance(payload.get("price_overrides"), list):
            raise CommandError("The source file must contain price_overrides.")
        return payload

    def _ensure_account_mappings(
        self,
        *,
        organization: Organization,
        effective_from: datetime.date,
        stats: dict[str, int],
    ) -> None:
        for role_code, account_code in STANDARD_SALES_ACCOUNT_MAPPINGS:
            role = AccountRole.objects.filter(code=role_code, is_active=True).first()
            account = Account.objects.filter(
                organization=organization,
                code=account_code,
                is_active=True,
                is_postable=True,
            ).first()
            if role is None or account is None:
                raise CommandError(
                    f"Accounting setup is missing role {role_code} or account {account_code}."
                )
            if OrganizationAccountMapping.objects.filter(
                organization=organization,
                account_role=role,
                is_active=True,
            ).exists():
                continue
            create_account_mapping(
                organization=organization,
                account_role=role,
                account=account,
                effective_from=effective_from,
            )
            stats["account_mappings_created"] += 1

    def _channel(
        self,
        *,
        organization: Organization,
        payload: dict[str, Any],
        stats: dict[str, int],
    ) -> SalesChannel:
        source = payload.get("channel") or {}
        code = str(source.get("code") or "APPLICATIONS")
        channel = SalesChannel.objects.filter(organization=organization, code=code).first()
        if channel is not None:
            if channel.category != SalesChannelCategory.DELIVERY_APPLICATION:
                raise CommandError(f"Channel {code} is not a delivery-application channel.")
            if not channel.is_active:
                raise CommandError(f"Channel {code} is archived.")
            return channel

        center_code = str(source.get("cost_center") or "DELIVERY")
        try:
            center = CostCenter.objects.get(
                organization=organization,
                code=center_code,
                is_active=True,
            )
        except CostCenter.DoesNotExist as exc:
            raise CommandError(f"Cost center {center_code} is missing.") from exc
        stats["channel_created"] += 1
        return create_sales_channel(
            organization=organization,
            code=code,
            name_ar=str(source.get("name_ar") or "التطبيقات"),
            name_en=str(source.get("name_en") or "Delivery applications"),
            category=SalesChannelCategory.DELIVERY_APPLICATION,
            cost_center=center,
            requires_cashier=False,
            display_order=int(source.get("display_order") or 4),
            notes="قناة موحدة لمبيعات تطبيقات التوصيل.",
        )

    def _applications(
        self,
        *,
        organization: Organization,
        branch: Branch,
        payload: dict[str, Any],
        effective_from: datetime.date,
        stats: dict[str, int],
    ) -> None:
        commission = self._decimal(payload.get("commission_percent"), "commission_percent")
        evidence = str(payload["evidence_reference"])
        basis = str(payload.get("commission_basis") or CommissionBasis.GROSS_LIST_AMOUNT)
        lag_days = int(payload.get("settlement_lag_days") or 30)

        seen_codes: set[str] = set()
        for source in payload["applications"]:
            code = str(source["code"]).strip().upper()
            if code in seen_codes:
                raise CommandError(f"Duplicate application code {code} in source file.")
            seen_codes.add(code)
            account = self._account(organization, source.get("receivable_account"))
            application = DeliveryApplication.objects.filter(
                organization=organization, code=code
            ).first()
            if application is None:
                application = create_delivery_application(
                    organization=organization,
                    code=code,
                    name_ar=str(source["name_ar"]),
                    name_en=str(source.get("name_en") or ""),
                    settlement_cycle_days=int(source.get("settlement_cycle_days") or lag_days),
                    receivable_account=account,
                    notes="شركة تطبيق توصيل فعلية.",
                )
                stats["applications_created"] += 1
            else:
                if not application.is_active:
                    raise CommandError(f"Application {code} is archived.")
                if application.name_ar != str(source["name_ar"]):
                    raise CommandError(f"Application {code} exists under another Arabic name.")
                stats["applications_reused"] += 1

            setting = DeliveryApplicationBranchSetting.objects.filter(
                delivery_application=application, branch=branch
            ).first()
            if setting is None:
                set_application_branch_setting(
                    application=application,
                    branch=branch,
                    is_active=True,
                    notes="مفعّل لمبيعات الفرع عبر التطبيق.",
                )
                stats["branch_settings_created"] += 1
            elif setting.is_active:
                stats["branch_settings_reused"] += 1
            else:
                set_application_branch_setting(
                    application=application,
                    branch=branch,
                    is_active=True,
                    notes=setting.notes,
                )
                stats["branch_settings_created"] += 1

            self._agreement(
                application=application,
                branch=branch,
                effective_from=effective_from,
                commission=commission,
                basis=basis,
                lag_days=lag_days,
                evidence=evidence,
                stats=stats,
            )

    def _agreement(
        self,
        *,
        application: DeliveryApplication,
        branch: Branch,
        effective_from: datetime.date,
        commission: Decimal,
        basis: str,
        lag_days: int,
        evidence: str,
        stats: dict[str, int],
    ) -> None:
        current = (
            DeliveryAgreement.objects.filter(
                delivery_application=application,
                branch=branch,
                is_active=True,
                effective_from__lte=effective_from,
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_from))
            .first()
        )
        if current is not None and (
            current.commission_percent == commission
            and current.fixed_fee_per_order == Decimal("0")
            and current.commission_basis == basis
        ):
            stats["agreements_reused"] += 1
            return
        if current is not None:
            if current.effective_from == effective_from:
                raise CommandError(
                    f"Application {application.code} has another same-day agreement."
                )
            close_delivery_agreement(
                agreement=current,
                effective_to=effective_from - datetime.timedelta(days=1),
                reason="تغيير عمولة التطبيق بتوجيه المالك.",
            )

        create_delivery_agreement(
            branch=branch,
            delivery_application=application,
            effective_from=effective_from,
            commission_percent=commission,
            commission_basis=basis,
            settlement_lag_days=lag_days,
            evidence_reference=evidence,
            notes="عمولة التطبيق حسب توجيه المالك.",
        )
        stats["agreements_created"] += 1

    def _prices(
        self,
        *,
        organization: Organization,
        branch: Branch,
        channel: SalesChannel,
        payload: dict[str, Any],
        effective_from: datetime.date,
        stats: dict[str, int],
    ) -> None:
        desired: dict[str, Decimal] = {}
        for source in payload["price_overrides"]:
            code = str(source["menu_item_code"])
            if code in desired:
                raise CommandError(f"Duplicate menu item {code} in price_overrides.")
            desired[code] = self._decimal(source.get("unit_price"), f"price for {code}")

        active_rows = list(
            MenuPriceVersion.objects.filter(
                branch=branch,
                scope=PriceScope.CHANNEL,
                channel=channel,
                is_active=True,
                effective_from__lte=effective_from,
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_from))
            .select_related("menu_item")
        )
        for current in active_rows:
            if current.menu_item.code in desired:
                continue
            if current.effective_from == effective_from:
                archive_menu_price(
                    price=current,
                    reason="هذا السعر يساوي سعر الصالة؛ يستخدم سعر الفرع الافتراضي.",
                )
                stats["prices_archived"] += 1
            else:
                close_menu_price(
                    price=current,
                    effective_to=effective_from - datetime.timedelta(days=1),
                    reason="العودة إلى سعر الفرع الافتراضي لهذا الصنف.",
                )
                stats["prices_closed"] += 1

        evidence = str(payload["evidence_reference"])
        for code, unit_price in desired.items():
            try:
                item = MenuItem.objects.get(organization=organization, code=code, is_active=True)
            except MenuItem.DoesNotExist as exc:
                raise CommandError(f"Menu item {code} is missing or archived.") from exc
            current_price = (
                MenuPriceVersion.objects.filter(
                    menu_item=item,
                    branch=branch,
                    scope=PriceScope.CHANNEL,
                    channel=channel,
                    is_active=True,
                    effective_from__lte=effective_from,
                )
                .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_from))
                .first()
            )
            if current_price is not None and current_price.unit_price == unit_price:
                stats["prices_reused"] += 1
                continue
            if current_price is not None:
                if current_price.effective_from == effective_from:
                    raise CommandError(f"Menu item {code} has another same-day channel price.")
                close_menu_price(
                    price=current_price,
                    effective_to=effective_from - datetime.timedelta(days=1),
                    reason="تغيير سعر قناة التطبيقات.",
                )
                stats["prices_closed"] += 1
            create_menu_price(
                menu_item=item,
                branch=branch,
                scope=PriceScope.CHANNEL,
                channel=channel,
                unit_price=unit_price,
                effective_from=effective_from,
                evidence_reference=evidence,
                notes="يختلف عن سعر الصالة حسب منيو التطبيقات.",
            )
            stats["prices_created"] += 1

    def _account(self, organization: Organization, code: object) -> Account | None:
        if not code:
            return None
        try:
            return Account.objects.get(
                organization=organization,
                code=str(code),
                is_active=True,
                is_postable=True,
            )
        except Account.DoesNotExist as exc:
            raise CommandError(f"Receivable account {code} is missing.") from exc

    def _decimal(self, value: object, field: str) -> Decimal:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CommandError(f"{field} must be a decimal number.") from exc
        if number < 0:
            raise CommandError(f"{field} cannot be negative.")
        return number
