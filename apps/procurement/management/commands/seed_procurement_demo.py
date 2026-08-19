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
from apps.procurement.demo import (
    seed_demo_account_mappings,
    seed_demo_award,
    seed_demo_catalogue,
    seed_demo_credit_notes,
    seed_demo_invoices,
    seed_demo_matches,
    seed_demo_order_revision,
    seed_demo_orders,
    seed_demo_payments,
    seed_demo_quotations,
    seed_demo_receipts,
    seed_demo_requests,
    seed_demo_returns,
    seed_demo_suppliers,
)
from apps.users.models import User

#: The demo storekeeper the inventory seed creates. Requests are raised by
#: them and decided by the signed-in user, so the two actors differ and the
#: maker-checker constraint is satisfied by real people rather than a
#: workaround.
CHECKER_USERNAME = "demo-storekeeper"


#: The screens this dataset makes reviewable, in navigation order. Route
#: names rather than literal paths: a hard-coded path is a second copy of
#: the URL configuration, and the copy is the one that goes stale.
INSPECTION_ROUTES: list[tuple[str, str]] = [
    ("procurement:supplier_list", "الموردون"),
    ("procurement:supplier_item_list", "كتالوج الموردين"),
    ("procurement:purchase_request_list", "طلبات الشراء"),
    ("procurement:quotation_list", "عروض الموردين"),
    ("procurement:purchase_order_list", "أوامر الشراء"),
    ("procurement:goods_receipt_list", "استلام البضاعة"),
    ("procurement:supplier_invoice_list", "فواتير الموردين"),
    ("procurement:supplier_invoice_charge_list", "التكاليف الإضافية"),
    ("procurement:matching_queue", "قائمة المطابقة"),
    ("procurement:purchase_match_list", "مطابقة المشتريات"),
    ("procurement:supplier_return_list", "مرتجعات الموردين"),
    ("procurement:supplier_credit_note_list", "إشعارات الموردين الدائنة"),
    ("procurement:supplier_payment_list", "دفعات الموردين"),
    ("procurement:report_supplier_aging", "أعمار ذمم الموردين"),
    ("procurement:report_supplier_statement", "كشف حساب مورد"),
    ("procurement:report_open_purchase_orders", "أوامر الشراء المفتوحة"),
    ("procurement:report_outstanding_receipts", "كميات مطلوبة غير مستلمة"),
    ("procurement:report_grni_exceptions", "مستلم غير مفوتر"),
    ("procurement:report_invoice_without_receipt", "مفوتر غير مستلم"),
    ("procurement:report_matching_exceptions", "فروقات المطابقة القائمة"),
    ("procurement:report_purchase_spend", "الإنفاق الشرائي"),
    ("procurement:report_price_variance", "فروقات الأسعار المرحّلة"),
    ("procurement:report_return_credit_status", "حالة المرتجعات والإشعارات"),
    ("procurement:report_payment_allocations", "تخصيصات الدفعات"),
    ("procurement:report_procurement_to_gl", "المشتريات إلى الأستاذ العام"),
    # Task 2.17 — the batches live on the shared framework, so the screen
    # is inventory's; what it now lists includes procurement's kinds.
    ("inventory:import_list", "سجل الاستيراد"),
]


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
            catalogue = seed_demo_catalogue(organization=organization)
            # Two actors, because maker-checker is a database constraint
            # and a demo that approved its own request would not insert.
            approver = User.objects.filter(username=CHECKER_USERNAME).first()
            requests = (
                seed_demo_requests(organization=organization, requester=approver, approver=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            quotations = seed_demo_quotations(organization=organization, recorder=user)
            awarded = seed_demo_award(organization=organization, approver=user)
            orders = (
                seed_demo_orders(organization=organization, preparer=approver, approver=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            seed_demo_order_revision(organization=organization, actor=user)
            receipts = (
                seed_demo_receipts(organization=organization, receiver=approver, inspector=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            # The payable role has to be mapped before an invoice can post.
            seed_demo_account_mappings(organization=organization)
            invoices = (
                seed_demo_invoices(organization=organization, recorder=approver, approver=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            matches = seed_demo_matches(organization=organization, matcher=user)
            returns = (
                seed_demo_returns(organization=organization, storekeeper=approver, manager=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            credit_notes = (
                seed_demo_credit_notes(organization=organization, recorder=approver, poster=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            payments = (
                seed_demo_payments(organization=organization, recorder=approver, poster=user)
                if approver is not None and approver.pk != user.pk
                else []
            )
            # Task 2.17: two supplier batches, so the import history at
            # /inventory/imports/ shows both halves of the contract — one
            # applied with a visible change, one refused with a good row in
            # it that was not quietly applied anyway.
            from apps.procurement.demo import (
                seed_demo_import_batch,
                seed_demo_rejected_import_batch,
            )

            import_batches = [
                batch
                for batch in (
                    seed_demo_import_batch(organization=organization),
                    seed_demo_rejected_import_batch(organization=organization),
                )
                if batch is not None
            ]

        self.write("")
        self.write(f"Organization  {organization.code} — {organization.name_ar}")
        self.write("suppliers:")
        for supplier in suppliers:
            self.write(f"  {supplier.code:<24} {supplier.name_ar}")
        self.write("")
        self.write("catalogue:")
        for row in catalogue:
            package = row.package_unit.code if row.package_unit else row.item.base_unit.code
            self.write(f"  {row.supplier.code:<24} {row.item.code:<16} {package}")
        self.write("")
        if requests:
            self.write("")
            self.write("purchase requests:")
            for document in requests:
                label = document.number or "draft"
                self.write(f"  {label:<24} {document.get_status_display()}")
        self.write("")
        if quotations:
            self.write("")
            self.write("quotations:")
            for quotation in quotations:
                self.write(
                    f"  {quotation.number:<24} {quotation.supplier.code:<24} "
                    f"{quotation.total_amount}"
                )
        if awarded is not None and awarded.awarded_quotation is not None:
            self.write("")
            self.write(
                f"award: {awarded.number} -> {awarded.awarded_quotation.number} "
                f"({awarded.awarded_quotation.supplier.code})"
            )
        self.write("")
        self.write(
            f"{len(suppliers)} suppliers, {len(catalogue)} catalogue rows, "
            f"{len(requests)} requests, {len(quotations)} quotations and "
            f"{len(orders)} orders, {len(receipts)} receipts and "
            f"{len(invoices)} supplier invoices, {len(matches)} matches, "
            f"{len(returns)} supplier returns, {len(credit_notes)} credit notes and "
            f"{len(payments)} payments present."
        )
        if matches:
            self.write("")
            self.write("purchase matches:")
            for match in matches:
                label = match.number or "draft"
                self.write(f"  {match.notes:<24} {label:<18} {match.get_status_display()}")
        if invoices:
            self.write("")
            self.write("supplier invoices:")
            for invoice in invoices:
                label = invoice.number or "draft"
                self.write(
                    f"  {invoice.supplier_invoice_number:<22} {label:<18} "
                    f"{invoice.get_status_display()}"
                )
        if returns:
            self.write("")
            self.write("supplier returns:")
            for supplier_return in returns:
                label = supplier_return.number or "draft"
                self.write(
                    f"  {supplier_return.evidence_reference:<22} {label:<18} "
                    f"{supplier_return.get_status_display()}"
                )
        if credit_notes:
            self.write("")
            self.write("supplier credit notes:")
            for note in credit_notes:
                label = note.number or "draft"
                self.write(
                    f"  {note.supplier_document_number:<22} {label:<18} {note.get_status_display()}"
                )
        if payments:
            self.write("")
            self.write("supplier payments:")
            for payment in payments:
                label = payment.number or "draft"
                self.write(f"  {payment.reference:<22} {label:<18} {payment.get_status_display()}")
        if import_batches:
            self.write("")
            self.write("import batches:")
            for batch in import_batches:
                counts = f"{batch.valid_row_count} صالح / {batch.error_row_count} مرفوض"
                applied = f", طُبِّق {batch.applied_row_count}" if batch.applied_at else ""
                self.write(
                    f"  {batch.original_filename:<30} "
                    f"{batch.get_status_display():<12} {counts}{applied}"
                )
        self.write("")
        self.write("Screens to inspect (python manage.py runserver, then):")
        for route, label in INSPECTION_ROUTES:
            self.write(f"  http://127.0.0.1:8000{reverse(route):<34} {label}")
