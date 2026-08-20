from __future__ import annotations

import datetime
import json
from decimal import Decimal
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.accounting.models import (
    GOODS_RECEIVED_NOT_INVOICED,
    INVENTORY_CONTROL,
    PURCHASE_PRICE_VARIANCE,
    SUPPLIER_PAYABLE,
    Account,
    AccountReportMapping,
    AccountRole,
    FiscalYear,
    OrganizationAccountMapping,
)
from apps.accounting.reports import default_report_group
from apps.accounting.services import (
    configure_accounting,
    create_account_mapping,
    open_fiscal_year,
    set_report_mapping,
)
from apps.inventory.models import Warehouse
from apps.procurement.invoices import approve_supplier_invoice, post_supplier_invoice
from apps.procurement.management.commands.import_legacy_purchase_invoices import IMPORT_TAG
from apps.procurement.matching import add_allocation, create_purchase_match, mark_match_ready
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptStatus,
    PurchaseMatchStatus,
    SupplierInvoice,
    SupplierInvoiceStatus,
)
from apps.procurement.posting import post_goods_receipt
from apps.procurement.services import (
    add_receipt_line,
    create_goods_receipt,
    inspect_receipt_line,
)
from apps.users.models import User

POSTING_TAG = "legacy-purchase-posting-v1"

# Only the roles exercised by a goods receipt and its matched supplier invoice.
# The chart itself is the complete standard chart; these mappings make no
# decisions for unrelated modules.
REQUIRED_ACCOUNT_MAPPINGS: tuple[tuple[str, str], ...] = (
    (INVENTORY_CONTROL, "1-03-01-001"),
    (GOODS_RECEIVED_NOT_INVOICED, "2-01-02-001"),
    (SUPPLIER_PAYABLE, "2-01-01-001"),
    (PURCHASE_PRICE_VARIANCE, "8-01-03-001"),
)

ZERO = Decimal("0.000")


class Command(BaseCommand):
    help = (
        "Post the imported legacy purchase invoices through real goods receipts, "
        "stock movements, matching, and supplier-payable journals."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--username", default="moamel")
        parser.add_argument("--branch-code", default="011")
        parser.add_argument("--warehouse-code", default="MAIN")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Persist the posting. Without this flag the entire run is rolled back.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            actor = User.objects.get(username=options["username"], is_active=True)
            warehouse = Warehouse.objects.select_related("branch", "branch__organization").get(
                branch__code=options["branch_code"],
                code=options["warehouse_code"],
                is_active=True,
            )
        except (User.DoesNotExist, Warehouse.DoesNotExist) as exc:
            raise CommandError(f"Required posting setup is missing: {exc}") from exc

        branch = warehouse.branch
        organization = branch.organization
        invoices = list(
            SupplierInvoice.objects.filter(
                organization=organization,
                branch=branch,
                notes__contains=IMPORT_TAG,
            )
            .select_related("supplier", "branch", "organization")
            .prefetch_related("lines__item")
            .order_by("business_date", "id")
        )
        if not invoices:
            raise CommandError(f"No invoices tagged {IMPORT_TAG!r} were found.")

        stats: dict[str, int | str] = {
            "invoices_found": len(invoices),
            "invoices_posted": 0,
            "invoices_skipped": 0,
            "receipts_posted": 0,
            "receipt_lines": 0,
            "matches_readied": 0,
            "allocations": 0,
            "report_mappings_created": 0,
            "receipt_value": "0.000",
            "invoice_value": "0.000",
            "price_variance": "0.000",
        }

        with transaction.atomic():
            stats["report_mappings_created"] = self._ensure_accounting(invoices=invoices)
            receipt_value = ZERO
            invoice_value = ZERO
            price_variance = ZERO

            for invoice in invoices:
                if invoice.status == SupplierInvoiceStatus.POSTED:
                    receipt, variance = self._validate_posted_invoice(invoice)
                    stats["invoices_skipped"] = int(stats["invoices_skipped"]) + 1
                elif invoice.status == SupplierInvoiceStatus.DRAFT:
                    receipt, variance = self._post_invoice(
                        invoice=invoice,
                        warehouse=warehouse,
                        actor=actor,
                        stats=stats,
                    )
                else:
                    raise CommandError(
                        f"Invoice {invoice.supplier_invoice_number} is in unexpected "
                        f"status {invoice.status}; complete or reverse that manual work first."
                    )

                receipt_value += receipt.posted_value or ZERO
                invoice_value += invoice.total_amount
                price_variance += variance

            stats["receipt_value"] = format(receipt_value, "f")
            stats["invoice_value"] = format(invoice_value, "f")
            stats["price_variance"] = format(price_variance, "f")
            if invoice_value - receipt_value != price_variance:
                raise CommandError(
                    "Batch reconciliation failed: invoice value minus receipt value "
                    "does not equal purchase price variance."
                )

            if not options["commit"]:
                transaction.set_rollback(True)

        mode = "COMMITTED" if options["commit"] else "DRY RUN (rolled back)"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {json.dumps(stats, sort_keys=True)}"))

    def _ensure_accounting(self, *, invoices: list[SupplierInvoice]) -> int:
        organization = invoices[0].organization
        configure_accounting(organization=organization, fiscal_year_start_month=1)
        call_command(
            "seed_chart_of_accounts",
            organization=organization.code,
            verbosity=0,
        )

        years = sorted({invoice.business_date.year for invoice in invoices})
        for year in years:
            if not FiscalYear.objects.filter(organization=organization, year=year).exists():
                open_fiscal_year(organization=organization, year=year)

        first_day = min(invoice.business_date for invoice in invoices)
        last_day = max(invoice.business_date for invoice in invoices)
        effective_from = datetime.date(min(years), 1, 1)
        for role_code, account_code in REQUIRED_ACCOUNT_MAPPINGS:
            role = AccountRole.objects.filter(code=role_code, is_active=True).first()
            account = Account.objects.filter(
                organization=organization,
                code=account_code,
                is_active=True,
                is_postable=True,
            ).first()
            if role is None or account is None:
                raise CommandError(
                    f"Accounting seed did not provide active role {role_code} "
                    f"and account {account_code}."
                )

            covering = OrganizationAccountMapping.objects.filter(
                organization=organization,
                account_role=role,
                is_active=True,
                effective_from__lte=first_day,
            ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=last_day))
            if covering.exists():
                continue
            if OrganizationAccountMapping.objects.filter(
                organization=organization, account_role=role
            ).exists():
                raise CommandError(
                    f"Existing mapping for {role_code} does not cover "
                    f"{first_day.isoformat()} through {last_day.isoformat()}."
                )
            create_account_mapping(
                organization=organization,
                account_role=role,
                account=account,
                effective_from=effective_from,
            )

        mappings_created = 0
        for account in Account.objects.filter(
            organization=organization, is_postable=True, is_active=True
        ).order_by("code"):
            existing = AccountReportMapping.objects.filter(
                organization=organization, account=account
            ).first()
            if existing is not None:
                if not existing.is_active:
                    raise CommandError(
                        f"Statement mapping for account {account.code} was deliberately "
                        "deactivated; review it instead of letting this import replace it."
                    )
                continue
            group, section = default_report_group(account)
            set_report_mapping(
                organization=organization,
                account=account,
                statement_group=group,
                presentation_section=section,
            )
            mappings_created += 1
        return mappings_created

    def _post_invoice(
        self,
        *,
        invoice: SupplierInvoice,
        warehouse: Warehouse,
        actor: User,
        stats: dict[str, int | str],
    ) -> tuple[GoodsReceipt, Decimal]:
        if GoodsReceipt.objects.filter(
            organization=invoice.organization,
            supplier=invoice.supplier,
            delivery_reference=invoice.supplier_invoice_number,
        ).exists():
            raise CommandError(
                f"A receipt already uses invoice reference {invoice.supplier_invoice_number}; "
                "refusing to duplicate or reinterpret it."
            )

        invoice_lines = list(invoice.lines.select_related("item").order_by("sequence"))
        if not invoice_lines:
            raise CommandError(f"Invoice {invoice.supplier_invoice_number} has no lines.")
        if any(line.item is None or line.base_quantity is None for line in invoice_lines):
            raise CommandError(
                f"Invoice {invoice.supplier_invoice_number} contains a non-inventory line."
            )

        receipt = create_goods_receipt(
            supplier=invoice.supplier,
            branch=invoice.branch,
            warehouse=warehouse,
            created_by=actor,
            received_at=invoice.business_date,
            delivery_reference=invoice.supplier_invoice_number,
            evidence_reference=invoice.supplier_reference,
            notes=(
                f"إدخال مخزني من فاتورة شراء مستوردة؛ {POSTING_TAG}; "
                f"فاتورة المورد: {invoice.supplier_invoice_number}"
            ),
        )

        receipt_lines = {}
        for invoice_line in invoice_lines:
            item = invoice_line.item
            quantity = invoice_line.base_quantity
            if item is None or quantity is None:  # pragma: no cover - batch guard above
                raise CommandError(
                    f"Invoice {invoice.supplier_invoice_number} contains a non-inventory line."
                )
            line = add_receipt_line(
                receipt=receipt,
                item=item,
                delivered_quantity=quantity,
                unit_price=invoice_line.unit_price,
                note=f"فاتورة المورد {invoice.supplier_invoice_number}، سطر {invoice_line.sequence}",
            )
            line = inspect_receipt_line(
                line=line,
                accepted_base_quantity=line.delivered_base_quantity,
                actor=actor,
                note="مقبول بالكامل من فاتورة الشراء الأصلية.",
            )
            receipt_lines[invoice_line.sequence] = line

        receipt = post_goods_receipt(receipt=receipt, actor=actor)
        invoice = approve_supplier_invoice(invoice=invoice, actor=actor)
        match = create_purchase_match(
            invoice=invoice,
            created_by=actor,
            notes=f"مطابقة آلية لفاتورة مستوردة؛ {POSTING_TAG}",
        )
        for invoice_line in invoice_lines:
            quantity = invoice_line.base_quantity
            if quantity is None:  # pragma: no cover - batch guard above
                raise CommandError(
                    f"Invoice {invoice.supplier_invoice_number} contains a non-inventory line."
                )
            add_allocation(
                match=match,
                invoice_line=invoice_line,
                receipt_line=receipt_lines[invoice_line.sequence],
                matched_base_quantity=quantity,
                created_by=actor,
                note="مطابقة كاملة مع سند الإدخال المخزني.",
            )
        match = mark_match_ready(match=match, actor=actor)
        invoice = post_supplier_invoice(invoice=invoice, actor=actor)

        receipt.refresh_from_db()
        invoice.refresh_from_db()
        match.refresh_from_db()
        posted_line_value = sum(
            (line.posted_value or ZERO for line in receipt.lines.all()), start=ZERO
        )
        if receipt.posted_value != posted_line_value:
            raise CommandError(
                f"Receipt value for {invoice.supplier_invoice_number} is "
                f"{receipt.posted_value}, but its posted lines total {posted_line_value}."
            )
        if invoice.posted_amount != invoice.total_amount:
            raise CommandError(
                f"Invoice {invoice.supplier_invoice_number} posted {invoice.posted_amount}, "
                f"expected {invoice.total_amount}."
            )

        stats["invoices_posted"] = int(stats["invoices_posted"]) + 1
        stats["receipts_posted"] = int(stats["receipts_posted"]) + 1
        stats["receipt_lines"] = int(stats["receipt_lines"]) + len(invoice_lines)
        stats["matches_readied"] = int(stats["matches_readied"]) + 1
        stats["allocations"] = int(stats["allocations"]) + len(invoice_lines)
        return receipt, match.total_price_variance

    def _validate_posted_invoice(self, invoice: SupplierInvoice) -> tuple[GoodsReceipt, Decimal]:
        receipt = (
            GoodsReceipt.objects.filter(
                organization=invoice.organization,
                supplier=invoice.supplier,
                delivery_reference=invoice.supplier_invoice_number,
                notes__contains=POSTING_TAG,
            )
            .prefetch_related("lines")
            .first()
        )
        match = invoice.matches.exclude(status=PurchaseMatchStatus.CANCELLED).first()
        expected_lines = invoice.lines.count()
        if (
            receipt is None
            or receipt.status != GoodsReceiptStatus.POSTED
            or receipt.lines.count() != expected_lines
            or match is None
            or match.status != PurchaseMatchStatus.READY
            or match.allocations.count() != expected_lines
            or invoice.posted_amount != invoice.total_amount
        ):
            raise CommandError(
                f"Posted invoice {invoice.supplier_invoice_number} does not match the "
                f"{POSTING_TAG} receipt/match evidence."
            )
        return receipt, match.total_price_variance
