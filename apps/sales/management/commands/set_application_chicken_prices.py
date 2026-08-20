from __future__ import annotations

import datetime
import json
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.accounting.models import CostCenter
from apps.organizations.models import Branch, Organization
from apps.sales.models import (
    MenuItem,
    MenuPriceVersion,
    PriceScope,
    SalesChannel,
    SalesChannelCategory,
)
from apps.sales.services import (
    close_menu_price,
    create_menu_price,
    create_sales_channel,
)

EXCEPTION_HALF_CODES = {
    "P-MADFOON-DAJAJ-HALF",
    "P-MANDI-DAJAJ-HALF",
}


class Command(BaseCommand):
    help = "Set effective-dated channel prices for whole and half chicken menu items."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--branch", required=True)
        parser.add_argument("--effective-from", required=True, help="YYYY-MM-DD")
        parser.add_argument("--full-price", required=True, type=Decimal)
        parser.add_argument("--half-price", required=True, type=Decimal)
        parser.add_argument("--exception-half-price", required=True, type=Decimal)
        parser.add_argument("--channel-code", default="APPLICATIONS")
        parser.add_argument("--channel-name", default="التطبيقات")
        parser.add_argument("--cost-center", default="DELIVERY")
        parser.add_argument("--evidence", required=True)
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist the prices. Without this flag the complete run is rolled back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            effective_from = datetime.date.fromisoformat(options["effective_from"])
        except ValueError as exc:
            raise CommandError("--effective-from must use YYYY-MM-DD.") from exc

        prices = {
            "full": options["full_price"],
            "half": options["half_price"],
            "exception_half": options["exception_half_price"],
        }
        if any(price < 0 for price in prices.values()):
            raise CommandError("Prices cannot be negative.")

        try:
            organization = Organization.objects.get(code=options["organization"])
            branch = Branch.objects.get(
                organization=organization,
                code=options["branch"],
            )
            cost_center = CostCenter.objects.get(
                organization=organization,
                code=options["cost_center"],
                is_active=True,
            )
        except (Organization.DoesNotExist, Branch.DoesNotExist, CostCenter.DoesNotExist) as exc:
            raise CommandError(f"Required organization setup is missing: {exc}") from exc

        stats = {
            "channel_created": 0,
            "items_found": 0,
            "prices_created": 0,
            "prices_closed": 0,
            "prices_reused": 0,
        }
        with transaction.atomic():
            channel = self._channel(
                organization=organization,
                code=options["channel_code"],
                name=options["channel_name"],
                cost_center=cost_center,
                stats=stats,
            )
            items = list(
                MenuItem.objects.filter(
                    organization=organization,
                    is_active=True,
                    name_ar__contains="دجاج",
                )
                .filter(Q(code__endswith="-FULL") | Q(code__endswith="-HALF"))
                .order_by("code")
            )
            item_codes = {item.code for item in items}
            missing_exceptions = EXCEPTION_HALF_CODES - item_codes
            if missing_exceptions:
                raise CommandError(
                    "Required exception menu items are missing: "
                    + ", ".join(sorted(missing_exceptions))
                )
            if not items:
                raise CommandError("No whole or half chicken menu items were found.")
            stats["items_found"] = len(items)

            for item in items:
                price = self._price_for(item=item, prices=prices)
                self._set_price(
                    item=item,
                    branch=branch,
                    channel=channel,
                    effective_from=effective_from,
                    unit_price=price,
                    evidence=options["evidence"],
                    stats=stats,
                )

            if not options["commit"]:
                transaction.set_rollback(True)

        mode = "COMMITTED" if options["commit"] else "DRY RUN (rolled back)"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {json.dumps(stats, sort_keys=True)}"))

    def _channel(
        self,
        *,
        organization: Organization,
        code: str,
        name: str,
        cost_center: CostCenter,
        stats: dict[str, int],
    ) -> SalesChannel:
        channel = SalesChannel.objects.filter(organization=organization, code=code).first()
        if channel is not None:
            if channel.category != SalesChannelCategory.DELIVERY_APPLICATION:
                raise CommandError(
                    f"Sales channel {code} exists with category {channel.category}, "
                    "not DELIVERY_APPLICATION."
                )
            if not channel.is_active:
                raise CommandError(f"Sales channel {code} is archived.")
            return channel

        stats["channel_created"] += 1
        return create_sales_channel(
            organization=organization,
            code=code,
            name_ar=name,
            name_en="Delivery applications",
            category=SalesChannelCategory.DELIVERY_APPLICATION,
            cost_center=cost_center,
            requires_cashier=False,
            display_order=4,
            notes="قناة موحدة لأسعار تطبيقات التوصيل.",
        )

    def _price_for(self, *, item: MenuItem, prices: dict[str, Decimal]) -> Decimal:
        if item.code.endswith("-FULL"):
            return prices["full"]
        if item.code in EXCEPTION_HALF_CODES:
            return prices["exception_half"]
        return prices["half"]

    def _set_price(
        self,
        *,
        item: MenuItem,
        branch: Branch,
        channel: SalesChannel,
        effective_from: datetime.date,
        unit_price: Decimal,
        evidence: str,
        stats: dict[str, int],
    ) -> None:
        future_exists = MenuPriceVersion.objects.filter(
            menu_item=item,
            branch=branch,
            scope=PriceScope.CHANNEL,
            channel=channel,
            is_active=True,
            effective_from__gt=effective_from,
        ).exists()
        if future_exists:
            raise CommandError(f"{item.code} already has a future application-channel price.")

        current = (
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
        if current is not None and current.unit_price == unit_price:
            stats["prices_reused"] += 1
            return
        if current is not None:
            if current.effective_from == effective_from:
                raise CommandError(
                    f"{item.code} already has a different channel price starting "
                    f"{effective_from.isoformat()}; do not rewrite same-day price history."
                )
            close_menu_price(
                price=current,
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
            notes="سعر خاص بقناة تطبيقات التوصيل.",
        )
        stats["prices_created"] += 1
