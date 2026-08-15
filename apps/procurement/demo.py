"""
Procurement demo data.

Three suppliers, and no more. The point of a demo dataset is that every screen
has something recognisable on it — not that it looks busy. Thirty suppliers
would make the supplier list impressive and every later screen unreadable, and
the five inventory demo items already carry the item side of the story.

Namespaced `DEMO-`, idempotent by code, and written through the real services
so a rule the services enforce cannot be bypassed by the seed. `DEBUG` is
checked by the command, not here: this function is also called by tests, which
run with `DEBUG` off and must still be able to build the fixture.

See `docs/development/demo-data-policy.md`.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from apps.accounting.models import (
    PURCHASE_PRICE_VARIANCE,
    PURCHASE_RETURN_VARIANCE,
    SUPPLIER_ADVANCE,
    SUPPLIER_PAYABLE,
    SUPPLIER_PAYMENT_BANK,
    SUPPLIER_PAYMENT_CASH,
    SUPPLIER_RETURN_CLEARING,
    Account,
    AccountRole,
    CostCenter,
    OrganizationAccountMapping,
)
from apps.accounting.services import create_account_mapping
from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    PackageUnit,
    Warehouse,
)
from apps.organizations.models import Branch, Organization
from apps.procurement.comparison import award_quotation
from apps.procurement.credit_notes import (
    add_return_allocation,
    create_supplier_credit_note,
    post_supplier_credit_note,
)
from apps.procurement.invoices import (
    add_account_line,
    add_inventory_line,
    approve_supplier_invoice,
    create_supplier_invoice,
    post_supplier_invoice,
    reverse_supplier_invoice,
)
from apps.procurement.matching import (
    add_allocation,
    cancel_purchase_match,
    create_purchase_match,
    mark_match_ready,
)
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseMatch,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
    SupplierCreditNote,
    SupplierCreditNoteStatus,
    SupplierInvoice,
    SupplierInvoiceLineType,
    SupplierInvoiceStatus,
    SupplierItem,
    SupplierPayment,
    SupplierPaymentStatus,
    SupplierQuotation,
    SupplierQuotationStatus,
    SupplierReturn,
    SupplierReturnStatus,
)
from apps.procurement.payments import (
    add_payment_allocation,
    create_supplier_payment,
    post_supplier_payment,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
from apps.procurement.returns import (
    add_return_line,
    create_supplier_return,
    post_supplier_return,
    reverse_supplier_return,
)
from apps.procurement.services import (
    add_order_line,
    add_quotation_line,
    add_receipt_line,
    add_request_line,
    approve_purchase_order,
    approve_purchase_request,
    cancel_purchase_order,
    create_goods_receipt,
    create_purchase_order,
    create_purchase_request,
    create_supplier,
    create_supplier_item,
    create_supplier_quotation,
    inspect_receipt_line,
    issue_purchase_order,
    reject_purchase_request,
    revise_purchase_order,
    submit_purchase_request,
    submit_supplier_quotation,
)
from apps.users.models import User

#: code, Arabic name, English name, contact, phone, payment terms in days.
#:
#: The terms differ on purpose. Meat is bought on thirty days, chicken on
#: fourteen, and groceries cash on delivery — so the aging report has three
#: different answers to show the day it is built, instead of one repeated
#: three times.
DEMO_SUPPLIERS: list[tuple[str, str, str, str, str, int]] = [
    (
        "DEMO-MEAT-SUPPLIER",
        "مورد اللحوم — تجريبي",
        "Meat Supplier (demo)",
        "أبو علي",
        "07701111111",
        30,
    ),
    (
        "DEMO-CHICKEN-SUPPLIER",
        "مورد الدجاج — تجريبي",
        "Chicken Supplier (demo)",
        "أبو حسن",
        "07702222222",
        14,
    ),
    (
        "DEMO-GROCERY-SUPPLIER",
        "مورد المواد الغذائية — تجريبي",
        "Grocery Supplier (demo)",
        "أم زينب",
        "07703333333",
        0,
    ),
]


def seed_demo_suppliers(*, organization: Organization) -> list[Supplier]:
    """
    Create the three demo suppliers, or return the ones already there.

    Idempotent by `(organization, code)`, which is the same key the database
    constraint uses — so a re-run cannot produce a duplicate even if this
    check were removed.
    """
    suppliers: list[Supplier] = []
    for code, name_ar, name_en, contact, phone, terms in DEMO_SUPPLIERS:
        existing = Supplier.objects.filter(organization=organization, code=code).first()
        if existing is not None:
            suppliers.append(existing)
            continue
        suppliers.append(
            create_supplier(
                organization=organization,
                code=code,
                name_ar=name_ar,
                name_en=name_en,
                contact_name=contact,
                phone=phone,
                payment_terms_days=terms,
            )
        )
    return suppliers


#: supplier code, item code, package code or None, supplier SKU, quoted price,
#: lead time in days, minimum order, preferred.
#:
#: Deliberately small: three suppliers against the five items the inventory
#: demo already created, and the mix chosen so each rule has one visible case.
#: The meat row is a `VARIABLE` container, so a receipt against it will demand
#: a measured weight; the rice row is a `FIXED` sack that converts
#: arithmetically; the chicken row carries no quoted price at all, because "we
#: have not agreed one" is a real state and not a zero.
DEMO_CATALOGUE: list[tuple[str, str, str | None, str, str | None, int | None, str | None, bool]] = [
    ("DEMO-MEAT-SUPPLIER", "DEMO-MEAT", "CONTAINER", "MT-CNT-18", "9500.000000", 2, "1.000", True),
    ("DEMO-CHICKEN-SUPPLIER", "DEMO-CHICKEN", "CARTON", "CHK-CTN-10", None, 1, "2.000", True),
    ("DEMO-GROCERY-SUPPLIER", "DEMO-RICE", "SACK", "RC-SACK-30", "42000.000000", 3, "5.000", True),
    ("DEMO-GROCERY-SUPPLIER", "DEMO-OIL", "CARTON", "OIL-CTN-20", "38000.000000", 3, "2.000", True),
    (
        "DEMO-GROCERY-SUPPLIER",
        "DEMO-CONTAINER",
        "CARTON",
        "PKG-CTN-500",
        "17500.000000",
        5,
        "1.000",
        True,
    ),
    # A second source for rice, quoted higher and not preferred. Without it the
    # comparison screen Task 2.5 builds would have nothing to compare, and the
    # one-preferred-per-item constraint would never be exercised by the demo.
    (
        "DEMO-MEAT-SUPPLIER",
        "DEMO-RICE",
        "SACK",
        "ALT-RICE-30",
        "43500.000000",
        1,
        "10.000",
        False,
    ),
]

#: The catalogue starts the day the inventory demo opened its stock, so the
#: whole demo tells one story on one timeline.
CATALOGUE_EFFECTIVE_FROM = datetime.date(2026, 1, 1)


#: The procurement roles the implemented postings resolve, and their demo
#: accounts. Procurement seeds its own rather than adding to the inventory
#: demo's list: `SUPPLIER_PAYABLE` is not an inventory concept, and a module
#: that maps another module's vocabulary is a dependency nobody declared.
#:
#: Two entries, because Tasks 2.10 and 2.12 post to two roles. The rest of Task
#: 2.0 §15 joins as the tasks that post to them land.
#:
#: The variance account is a **clearing** account, not cost of sales: Task 2.0
#: §15's `5-02-01-001` is superseded (ADR-022, amended at Task 2.12), because
#: class 5 would demand a cost centre a supplier invoice has nowhere to get and
#: because ADR-022 separately rejects booking a purchasing outcome as food cost.
PROCUREMENT_ACCOUNT_MAPPINGS: list[tuple[str, str]] = [
    (SUPPLIER_PAYABLE, "2-01-01-001"),
    (PURCHASE_PRICE_VARIANCE, "8-01-03-001"),
    (SUPPLIER_RETURN_CLEARING, "8-01-04-001"),
    # Task 2.14's first deliberate act: the role Task 2.13 seeded and refused
    # to map is mapped now, because the credit note posts to it.
    (PURCHASE_RETURN_VARIANCE, "7-09-04-001"),
    # Task 2.15: the payment sources by method, and the advance for the
    # unallocated remainder — all three posted to by the payment run.
    (SUPPLIER_PAYMENT_CASH, "1-01-01-001"),
    (SUPPLIER_PAYMENT_BANK, "1-01-02-001"),
    (SUPPLIER_ADVANCE, "1-04-01-001"),
]


def seed_demo_account_mappings(*, organization: Organization) -> list[str]:
    """
    Map the procurement roles to demo accounts, idempotently.

    Skips a role already mapped rather than superseding it — a seed that
    versioned its own mappings every run would grow an effective-dated history
    nobody wrote, and the resolver would start answering with a version number
    that means nothing.
    """
    mapped: list[str] = []
    for code, account_code in PROCUREMENT_ACCOUNT_MAPPINGS:
        role = AccountRole.objects.filter(code=code).first()
        account = Account.objects.filter(
            organization=organization, code=account_code, is_postable=True, is_active=True
        ).first()
        if role is None or account is None:
            continue
        if OrganizationAccountMapping.objects.filter(
            organization=organization, account_role=role
        ).exists():
            mapped.append(code)
            continue
        create_account_mapping(
            organization=organization,
            account_role=role,
            account=account,
            effective_from=CATALOGUE_EFFECTIVE_FROM,
        )
        mapped.append(code)
    return mapped


def seed_demo_catalogue(*, organization: Organization) -> list[SupplierItem]:
    """
    Link the three demo suppliers to the five demo items.

    Idempotent by `(supplier, item, package, effective_from)`. Re-running
    returns the existing rows rather than superseding them: a seed that
    versioned its own data every run would grow a history nobody wrote.
    """
    suppliers = {
        supplier.code: supplier for supplier in Supplier.objects.filter(organization=organization)
    }
    items = {item.code: item for item in InventoryItem.objects.filter(organization=organization)}
    packages = {
        package.code: package for package in PackageUnit.objects.filter(organization=organization)
    }

    rows: list[SupplierItem] = []
    for code, item_code, package_code, sku, price, lead, minimum, preferred in DEMO_CATALOGUE:
        supplier = suppliers.get(code)
        item = items.get(item_code)
        if supplier is None or item is None:
            # The inventory demo has not run, or ran against another
            # organization. Skipping is honest; inventing the item is not.
            continue
        package = packages.get(package_code) if package_code else None

        existing = SupplierItem.objects.filter(
            supplier=supplier,
            item=item,
            package_unit=package,
            effective_from=CATALOGUE_EFFECTIVE_FROM,
        ).first()
        if existing is not None:
            rows.append(existing)
            continue

        rows.append(
            create_supplier_item(
                supplier=supplier,
                item=item,
                package_unit=package,
                effective_from=CATALOGUE_EFFECTIVE_FROM,
                supplier_sku=sku,
                supplier_description=item.name_ar,
                last_quoted_price=Decimal(price) if price else None,
                lead_time_days=lead,
                minimum_order_quantity=Decimal(minimum) if minimum else None,
                is_preferred=preferred,
            )
        )
    return rows


#: Four requests, one per lifecycle outcome, so every branch of the state
#: machine has a visible example rather than a description.
#:
#: Two people are needed and not one. The maker-checker rule is a database
#: constraint, so a demo where the requester also approved would not merely
#: look wrong — it would fail to insert, which is the point of building the
#: demo through the real services.
def seed_demo_requests(
    *, organization: Organization, requester: User, approver: User
) -> list[PurchaseRequest]:
    """
    A draft, a submitted, an approved and a rejected request.

    Idempotent by purpose text within the organization: re-running returns
    what is there rather than raising a second set. A request carries no
    ledger effect, so nothing here is irreversible — but a demo that doubled
    its own data every run would still be useless by the third run.
    """
    branch = Branch.objects.filter(organization=organization, code="DEMO-BUNOOK").first()
    if branch is None:
        return []
    warehouse = Warehouse.objects.filter(branch=branch, code="DEMO-MAIN", is_system=False).first()
    rice = InventoryItem.objects.filter(organization=organization, code="DEMO-RICE").first()
    oil = InventoryItem.objects.filter(organization=organization, code="DEMO-OIL").first()
    sack = PackageUnit.objects.filter(organization=organization, code="SACK").first()
    if warehouse is None or rice is None or oil is None:
        return []

    existing = list(
        PurchaseRequest.objects.filter(
            organization=organization, purpose__startswith="DEMO —"
        ).order_by("id")
    )
    if existing:
        return existing

    grocery = Supplier.objects.filter(
        organization=organization, code="DEMO-GROCERY-SUPPLIER"
    ).first()

    def build(purpose: str) -> PurchaseRequest:
        document = create_purchase_request(
            branch=branch,
            requested_by=requester,
            warehouse=warehouse,
            required_date=CATALOGUE_EFFECTIVE_FROM + datetime.timedelta(days=45),
            purpose=purpose,
        )
        add_request_line(
            request=document,
            item=rice,
            package_unit=sack,
            entered_quantity=Decimal("4.000"),
            preferred_supplier=grocery,
            note="مخزون الرز منخفض",
        )
        add_request_line(request=document, item=oil, entered_quantity=Decimal("60.000"))
        return document

    drafted = build("DEMO — قائمة الأسبوع القادم")

    submitted = build("DEMO — طلب بانتظار الاعتماد")
    submitted = submit_purchase_request(request=submitted, actor=requester)

    approved = build("DEMO — طلب معتمد")
    approved = submit_purchase_request(request=approved, actor=requester)
    approved = approve_purchase_request(
        request=approved, actor=approver, reason="الكميات ضمن المعدل الشهري"
    )

    rejected = build("DEMO — طلب مرفوض")
    rejected = submit_purchase_request(request=rejected, actor=requester)
    rejected = reject_purchase_request(
        request=rejected, actor=approver, reason="المخزون الحالي يكفي حتى نهاية الشهر"
    )

    return [drafted, submitted, approved, rejected]


def seed_demo_quotations(*, organization: Organization, recorder: User) -> list[SupplierQuotation]:
    """
    Two offers for the same approved request, from two suppliers.

    Two, and deliberately not one. A single quotation makes the comparison
    screen in Task 2.5 look like it works while proving nothing: the whole
    point is that two suppliers quoting **different package sizes** are only
    comparable once both are normalised to base units. So the grocery supplier
    quotes rice by the 30 kg sack and the meat supplier quotes the same rice in
    kilograms, at prices that reverse their ranking once freight is included —
    which is exactly the case a buyer has to be shown rather than told.
    """
    approved = (
        PurchaseRequest.objects.filter(
            organization=organization,
            status=PurchaseRequestStatus.APPROVED,
            purpose__startswith="DEMO —",
        )
        .order_by("id")
        .first()
    )
    if approved is None:
        return []

    existing = list(
        SupplierQuotation.objects.filter(
            organization=organization, supplier_reference__startswith="DEMO-Q"
        ).order_by("id")
    )
    if existing:
        return existing

    grocery = Supplier.objects.filter(
        organization=organization, code="DEMO-GROCERY-SUPPLIER"
    ).first()
    meat = Supplier.objects.filter(organization=organization, code="DEMO-MEAT-SUPPLIER").first()
    rice = InventoryItem.objects.filter(organization=organization, code="DEMO-RICE").first()
    sack = PackageUnit.objects.filter(organization=organization, code="SACK").first()
    if grocery is None or meat is None or rice is None or sack is None:
        return []

    quoted_on = approved.required_date - datetime.timedelta(days=20)

    # By the sack: 42,000 per 30 kg is 1,400 per kg, plus 15,000 delivery.
    cheaper_per_unit = create_supplier_quotation(
        supplier=grocery,
        recorded_by=recorder,
        request=approved,
        quoted_at=quoted_on,
        # Two years, not sixty days. A demo dataset is read months after it
        # is seeded, and an offer that quietly expires makes the comparison
        # screen show nothing awardable — which looks like a broken feature
        # rather than the honest state it is.
        valid_until=quoted_on + datetime.timedelta(days=730),
        supplier_reference="DEMO-Q-GROC-001",
        freight_amount=Decimal("15000.000"),
        evidence_reference="بريد المورد بتاريخ العرض",
    )
    add_quotation_line(
        quotation=cheaper_per_unit,
        item=rice,
        package_unit=sack,
        quantity=Decimal("4.000"),
        unit_price=Decimal("42000.000000"),
    )
    cheaper_per_unit = submit_supplier_quotation(quotation=cheaper_per_unit, actor=recorder)

    # By the kilogram: 1,450 per kg, delivered free. Dearer per unit, cheaper
    # landed — which is the whole reason freight is shown separately as well as
    # inside the comparison.
    free_delivery = create_supplier_quotation(
        supplier=meat,
        recorded_by=recorder,
        request=approved,
        quoted_at=quoted_on,
        # No stated expiry at all, which is the other real case and lets the
        # comparison show both.
        supplier_reference="DEMO-Q-MEAT-001",
        evidence_reference="عرض مكتوب مسلّم باليد",
    )
    add_quotation_line(
        quotation=free_delivery,
        item=rice,
        quantity=Decimal("120.000"),
        unit_price=Decimal("1450.000000"),
    )
    free_delivery = submit_supplier_quotation(quotation=free_delivery, actor=recorder)

    return [cheaper_per_unit, free_delivery]


def seed_demo_award(*, organization: Organization, approver: User) -> PurchaseRequest | None:
    """
    Award the dearer-per-unit offer, on purpose.

    The two demo quotations disagree: DEMO-GROCERY is cheaper per kilogram and
    DEMO-MEAT is cheaper once delivery is counted. Awarding the landed-cheapest
    one would make the demo look like the system picks a winner. Awarding it
    **with a stated reason** is the point — a buyer chose, and the reason is
    the record of why.
    """
    awarded = PurchaseRequest.objects.filter(
        organization=organization, awarded_quotation__isnull=False
    ).first()
    if awarded is not None:
        return awarded

    request = (
        PurchaseRequest.objects.filter(
            organization=organization,
            status=PurchaseRequestStatus.APPROVED,
            purpose__startswith="DEMO —",
        )
        .order_by("id")
        .first()
    )
    if request is None:
        return None

    winner = (
        SupplierQuotation.objects.filter(
            request=request,
            status=SupplierQuotationStatus.SUBMITTED,
            supplier__code="DEMO-MEAT-SUPPLIER",
        )
        .order_by("id")
        .first()
    )
    if winner is None:
        return None

    return award_quotation(
        request=request,
        quotation=winner,
        actor=approver,
        reason=("أغلى للكيلو لكن أرخص بعد النقل، والتسليم خلال يومين بدل ثلاثة."),
    )


def seed_demo_orders(
    *, organization: Organization, preparer: User, approver: User
) -> list[PurchaseOrder]:
    """
    Three orders: a draft, one issued from the award, and one cancelled.

    Two actors again, because approving an order is a spending commitment and
    the preparer is refused by a database constraint. The issued one carries
    the award from Task 2.5, so the chain request → quotation → award → order
    is visible end to end rather than described.
    """
    existing = list(
        PurchaseOrder.objects.filter(
            organization=organization, supplier_reference__startswith="DEMO-PO"
        ).order_by("id")
    )
    if existing:
        return existing

    branch = Branch.objects.filter(organization=organization, code="DEMO-BUNOOK").first()
    warehouse = Warehouse.objects.filter(branch=branch, code="DEMO-MAIN", is_system=False).first()
    rice = InventoryItem.objects.filter(organization=organization, code="DEMO-RICE").first()
    oil = InventoryItem.objects.filter(organization=organization, code="DEMO-OIL").first()
    sack = PackageUnit.objects.filter(organization=organization, code="SACK").first()
    if branch is None or warehouse is None or rice is None or oil is None:
        return []

    awarded_request = PurchaseRequest.objects.filter(
        organization=organization, awarded_quotation__isnull=False
    ).first()
    grocery = Supplier.objects.filter(
        organization=organization, code="DEMO-GROCERY-SUPPLIER"
    ).first()
    if grocery is None:
        return []

    ordered_on = CATALOGUE_EFFECTIVE_FROM + datetime.timedelta(days=30)
    orders: list[PurchaseOrder] = []

    # 1. A draft nobody has approved yet.
    drafted = create_purchase_order(
        supplier=grocery,
        branch=branch,
        warehouse=warehouse,
        created_by=preparer,
        ordered_on=ordered_on,
        expected_on=ordered_on + datetime.timedelta(days=7),
        supplier_reference="DEMO-PO-DRAFT",
    )
    add_order_line(
        order=drafted,
        item=oil,
        ordered_quantity=Decimal("40.000"),
        unit_price=Decimal("1900.000000"),
    )
    orders.append(drafted)

    # 2. The one that came from the award, approved and sent.
    if awarded_request is not None and awarded_request.awarded_quotation is not None:
        winner = awarded_request.awarded_quotation
        issued = create_purchase_order(
            supplier=winner.supplier,
            branch=branch,
            warehouse=warehouse,
            created_by=preparer,
            ordered_on=ordered_on,
            expected_on=ordered_on + datetime.timedelta(days=2),
            request=awarded_request,
            quotation=winner,
            supplier_reference="DEMO-PO-AWARDED",
        )
        add_order_line(
            order=issued,
            item=rice,
            ordered_quantity=Decimal("120.000"),
            unit_price=Decimal("1450.000000"),
        )
        issued = approve_purchase_order(order=issued, actor=approver)
        issued = issue_purchase_order(order=issued, actor=preparer)
        orders.append(issued)

    # 3. One withdrawn, so the terminal state has a visible example.
    withdrawn = create_purchase_order(
        supplier=grocery,
        branch=branch,
        warehouse=warehouse,
        created_by=preparer,
        ordered_on=ordered_on,
        supplier_reference="DEMO-PO-CANCELLED",
    )
    add_order_line(
        order=withdrawn,
        item=rice,
        package_unit=sack,
        ordered_quantity=Decimal("2.000"),
        unit_price=Decimal("43000.000000"),
    )
    orders.append(
        cancel_purchase_order(
            order=withdrawn, actor=approver, reason="المورد اعتذر عن التوريد هذا الشهر"
        )
    )
    return orders


def seed_demo_order_revision(*, organization: Organization, actor: User) -> PurchaseOrder | None:
    """
    Revise the issued demo order once, so both versions are visible.

    The change is a real one a buyer would make: the supplier confirms a
    smaller quantity than was ordered. Version 1 keeps what they were first
    told; the live row says what was agreed in the end.
    """
    order = PurchaseOrder.objects.filter(
        organization=organization, supplier_reference="DEMO-PO-AWARDED"
    ).first()
    if order is None or order.versions.exists():
        return order

    line = order.lines.order_by("sequence").first()
    if line is None:
        return order

    return revise_purchase_order(
        order=order,
        actor=actor,
        reason="المورد أكّد توفر ١٠٠ كغم فقط من أصل ١٢٠",
        line_quantities={str(line.line_uid): Decimal("100.000")},
    )


def seed_demo_receipts(
    *, organization: Organization, receiver: User, inspector: User
) -> list[GoodsReceipt]:
    """
    Six deliveries, each showing something the others do not.

    1. Fully accepted against the issued order, **posted** — the ordinary case,
       and the one that puts stock and a GRNI credit into the books together.
    2. A partial receipt against the same order, left **DRAFT**, so the screen
       shows a real outstanding remainder rather than a contrived one and there
       is something to inspect and post by hand.
    3. Partly rejected and **posted** — three of twelve cartons were off. Ninety
       kilograms entered stock and thirty did not; the rejected thirty produce
       no movement, no GRNI and no payable (PRC-025).
    4. A `VARIABLE` meat container weighed on arrival and **posted**, because
       the planning factor is an estimate and the scale is the quantity
       (PRC-026).
    5. A direct market purchase with no order, posted and then **reversed**,
       so a reader can see that a reversal mirrors the original exactly rather
       than netting it off with a fresh valuation.
    6. The delivery the rice invoice is a bill for, **posted** at 1,400 against
       the 1,450 that invoice charges, so three-way matching has a real price
       variance to surface rather than a contrived zero.

    Every one of them goes through `post_goods_receipt`. Nothing here writes a
    `StockMovement` or a `JournalEntry` directly — a demo that did would be
    demonstrating a path the system does not actually have.
    """
    # Idempotent per receipt rather than all-or-nothing. An early return on
    # "some of these already exist" would leave a database seeded by an
    # earlier task permanently short of the ones this one adds, and would
    # never post the drafts it left behind. Idempotent means the second run
    # reaches the same state as the first, not that it does nothing.
    existing = {
        row.delivery_reference: row
        for row in GoodsReceipt.objects.filter(
            organization=organization, delivery_reference__startswith="DEMO-GRN"
        ).order_by("id")
    }

    branch = Branch.objects.filter(organization=organization, code="DEMO-BUNOOK").first()
    warehouse = Warehouse.objects.filter(branch=branch, code="DEMO-MAIN", is_system=False).first()
    if branch is None or warehouse is None:
        return []

    order = PurchaseOrder.objects.filter(
        organization=organization,
        supplier_reference="DEMO-PO-AWARDED",
        status=PurchaseOrderStatus.ISSUED,
    ).first()
    grocery = Supplier.objects.filter(
        organization=organization, code="DEMO-GROCERY-SUPPLIER"
    ).first()
    chicken_supplier = Supplier.objects.filter(
        organization=organization, code="DEMO-CHICKEN-SUPPLIER"
    ).first()
    meat_supplier = Supplier.objects.filter(
        organization=organization, code="DEMO-MEAT-SUPPLIER"
    ).first()
    rice = InventoryItem.objects.filter(organization=organization, code="DEMO-RICE").first()
    chicken = InventoryItem.objects.filter(organization=organization, code="DEMO-CHICKEN").first()
    meat = InventoryItem.objects.filter(organization=organization, code="DEMO-MEAT").first()
    carton = PackageUnit.objects.filter(organization=organization, code="CARTON").first()
    container = PackageUnit.objects.filter(organization=organization, code="CONTAINER").first()
    reason = InventoryReasonCode.objects.filter(organization=organization, is_active=True).first()
    if grocery is None or rice is None:
        return []

    received_on = CATALOGUE_EFFECTIVE_FROM + datetime.timedelta(days=32)
    receipts: list[GoodsReceipt] = []

    # 1 + 2. Two partial deliveries against the one issued order, which was
    # revised down to 100 kg — 60 posted, 40 left as a draft to inspect.
    if order is not None:
        order_line = order.lines.order_by("sequence").first()
        for index, quantity in enumerate((Decimal("60.000"), Decimal("40.000")), start=1):
            reference = f"DEMO-GRN-PARTIAL-{index}"
            if reference in existing:
                receipts.append(existing[reference])
                continue
            receipt = create_goods_receipt(
                supplier=order.supplier,
                branch=branch,
                warehouse=warehouse,
                created_by=receiver,
                received_at=received_on + datetime.timedelta(days=index),
                order=order,
                delivery_reference=f"DEMO-GRN-PARTIAL-{index}",
                evidence_reference="إشعار تسليم المورد",
            )
            line = add_receipt_line(
                receipt=receipt,
                item=order_line.item if order_line else rice,
                delivered_quantity=quantity,
                order_line=order_line,
            )
            inspect_receipt_line(line=line, accepted_base_quantity=quantity, actor=inspector)
            receipts.append(receipt)

    # 3. Partly rejected: three of twelve chicken cartons arrived warm.
    if "DEMO-GRN-REJECT" in existing:
        receipts.append(existing["DEMO-GRN-REJECT"])
    elif chicken_supplier is not None and chicken is not None and carton is not None:
        spoiled = create_goods_receipt(
            supplier=chicken_supplier,
            branch=branch,
            warehouse=warehouse,
            created_by=receiver,
            received_at=received_on,
            delivery_reference="DEMO-GRN-REJECT",
            evidence_reference="صورة إشعار التسليم",
        )
        lot = InventoryLot.objects.filter(item=chicken).order_by("id").first()
        line = add_receipt_line(
            receipt=spoiled,
            item=chicken,
            package_unit=carton,
            delivered_quantity=Decimal("12.000"),
            unit_price=Decimal("14000.000000"),
            lot=lot,
        )
        # 12 cartons × 10 kg = 120 kg delivered; 30 kg rejected.
        inspect_receipt_line(
            line=line,
            accepted_base_quantity=Decimal("90.000"),
            actor=inspector,
            rejection_reason=reason,
            note="ثلاث كراتين وصلت غير مبردة",
        )
        receipts.append(spoiled)

    # 4. A variable meat container: the scale decides, not the factor.
    if "DEMO-GRN-WEIGHED" in existing:
        receipts.append(existing["DEMO-GRN-WEIGHED"])
    elif meat_supplier is not None and meat is not None and container is not None:
        weighed = create_goods_receipt(
            supplier=meat_supplier,
            branch=branch,
            warehouse=warehouse,
            created_by=receiver,
            received_at=received_on,
            delivery_reference="DEMO-GRN-WEIGHED",
            evidence_reference="قصاصة الميزان",
        )
        meat_lot = InventoryLot.objects.filter(item=meat).order_by("id").first()
        line = add_receipt_line(
            receipt=weighed,
            item=meat,
            package_unit=container,
            delivered_quantity=Decimal("1.000"),
            # The planning factor says 18 kg; the scale said 17.4.
            measured_base_quantity=Decimal("17.400"),
            unit_price=Decimal("9500.000000"),
            lot=meat_lot,
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("17.400"), actor=inspector)
        receipts.append(weighed)

    # 5. A direct market purchase — no order, its own entered price (PRC-028).
    if "DEMO-GRN-REVERSED" in existing:
        receipts.append(existing["DEMO-GRN-REVERSED"])
    else:
        returned = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=warehouse,
            created_by=receiver,
            received_at=received_on,
            delivery_reference="DEMO-GRN-REVERSED",
            evidence_reference="وصل السوق",
            notes="شراء مباشر من السوق",
        )
        line = add_receipt_line(
            receipt=returned,
            item=rice,
            delivered_quantity=Decimal("10.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("10.000"), actor=inspector)
        receipts.append(returned)

    # 6. The delivery `DEMO-SINV-GOODS` is actually a bill for, posted at the
    # 1,400 the invoice then charges 1,450 against. Added by Task 2.11: the
    # goods invoice was written to cite a posted grocery delivery and there
    # was not one — the award went to the meat supplier, so every posted rice
    # delivery belonged to somebody else and the evidence link quietly
    # resolved to nothing. Without this, matching has nothing to demonstrate
    # and neither does the invoice it was written for.
    if "DEMO-GRN-MATCHED" in existing:
        receipts.append(existing["DEMO-GRN-MATCHED"])
    else:
        billed = create_goods_receipt(
            supplier=grocery,
            branch=branch,
            warehouse=warehouse,
            created_by=receiver,
            received_at=received_on,
            delivery_reference="DEMO-GRN-MATCHED",
            evidence_reference="إشعار تسليم المورد",
            notes="التسليم الذي تقابله فاتورة الرز",
        )
        line = add_receipt_line(
            receipt=billed,
            item=rice,
            delivered_quantity=Decimal("60.000"),
            unit_price=Decimal("1400.000000"),
        )
        inspect_receipt_line(line=line, accepted_base_quantity=Decimal("60.000"), actor=inspector)
        receipts.append(billed)

    _post_demo_receipts(receipts, actor=inspector)
    return receipts


#: Which demo deliveries post, and which stays a draft. `DEMO-GRN-PARTIAL-2` is
#: deliberately absent: a reader needs one receipt they can inspect and post
#: themselves, and a seed that posted everything would leave the two commands
#: with nothing to demonstrate.
_DEMO_RECEIPTS_TO_POST = frozenset(
    {
        "DEMO-GRN-PARTIAL-1",
        "DEMO-GRN-REJECT",
        "DEMO-GRN-WEIGHED",
        "DEMO-GRN-REVERSED",
        "DEMO-GRN-MATCHED",
    }
)


def _post_demo_receipts(receipts: list[GoodsReceipt], *, actor: User) -> None:
    """
    Post through the real command, and reverse the one that is meant to be.

    Idempotent by construction rather than by a flag: `post_goods_receipt`
    refuses a receipt that is not DRAFT, so a second seed run finds them all
    POSTED and skips. The kernel's source-identity uniqueness would refuse a
    duplicate even if this did not.
    """
    for receipt in receipts:
        if receipt.delivery_reference not in _DEMO_RECEIPTS_TO_POST:
            continue
        if receipt.status != GoodsReceiptStatus.DRAFT:
            continue
        post_goods_receipt(receipt=receipt, actor=actor)
        receipt.refresh_from_db()

    reversed_one = next(
        (row for row in receipts if row.delivery_reference == "DEMO-GRN-REVERSED"), None
    )
    if reversed_one is not None and reversed_one.status == GoodsReceiptStatus.POSTED:
        reverse_goods_receipt(
            receipt=reversed_one,
            actor=actor,
            reason="المورد استرجع البضاعة في نفس اليوم",
        )
        reversed_one.refresh_from_db()


#: The expense account a demo delivery charge belongs to, and the cost centre
#: it is charged to. Both are looked up in the seeded chart rather than
#: created, so a chart that does not carry them is skipped rather than
#: invented.
#:
#: Every expense account in this chart of accounts sets `requires_cost_center`,
#: which is a real policy rather than an accident: an expense nobody can
#: attribute to a part of the business is an expense nobody can manage. The
#: demo therefore has to name one, and naming `DELIVERY` for a delivery charge
#: is the honest answer.
DEMO_FREIGHT_ACCOUNT = "5-01-02-003"
DEMO_FREIGHT_COST_CENTER = "DELIVERY"


def seed_demo_invoices(
    *, organization: Organization, recorder: User, approver: User
) -> list[SupplierInvoice]:
    """
    Four invoices, each showing a state the others do not.

    1. `DEMO-SINV-EXPENSE` — a delivery charge with no goods on it. Every line
       has a complete accounting route, so it approves and **posts**:
       `Dr` the expense account, `Cr` supplier payable. This is the whole of
       what Task 2.10 can post.
    2. `DEMO-SINV-GOODS` — a bill for the rice that actually arrived, citing
       the posted delivery as evidence. It approves and then **waits**: the
       amount that clears GRNI is the matched receipt value, and matching is
       Task 2.11. The screen says so rather than leaving a reader guessing.
    3. `DEMO-SINV-DRAFT` — a draft with freight and a discount on it, so the
       allocation across lines is visible and there is something to edit.
    4. `DEMO-SINV-REVERSED` — posted and then reversed, so a reader can see
       that the payable goes back to nothing without any figure being edited.

    Idempotent per invoice rather than all-or-nothing: a second run reaches the
    same state as the first, which is what idempotent means. Nothing here
    writes a `JournalEntry` directly — every one goes through the real command.
    """
    existing = {
        row.supplier_invoice_number: row
        for row in SupplierInvoice.objects.filter(
            organization=organization, supplier_invoice_number__startswith="DEMO-SINV"
        ).order_by("id")
    }

    branch = Branch.objects.filter(organization=organization, code="DEMO-BUNOOK").first()
    grocery = Supplier.objects.filter(
        organization=organization, code="DEMO-GROCERY-SUPPLIER"
    ).first()
    chicken_supplier = Supplier.objects.filter(
        organization=organization, code="DEMO-CHICKEN-SUPPLIER"
    ).first()
    rice = InventoryItem.objects.filter(organization=organization, code="DEMO-RICE").first()
    freight_account = Account.objects.filter(
        organization=organization, code=DEMO_FREIGHT_ACCOUNT, is_postable=True, is_active=True
    ).first()
    cost_center = CostCenter.objects.filter(
        organization=organization, code=DEMO_FREIGHT_COST_CENTER, is_active=True
    ).first()
    if (
        branch is None
        or grocery is None
        or rice is None
        or freight_account is None
        or cost_center is None
    ):
        return []

    invoiced_on = CATALOGUE_EFFECTIVE_FROM + datetime.timedelta(days=40)
    invoices: list[SupplierInvoice] = []

    # 1. A pure expense invoice: the one shape Task 2.10 posts end to end.
    invoices.append(
        _demo_expense_invoice(
            existing=existing,
            supplier=grocery,
            branch=branch,
            recorder=recorder,
            approver=approver,
            account=freight_account,
            cost_center=cost_center,
            invoiced_on=invoiced_on,
        )
    )

    # 2. A goods invoice against the posted rice delivery. Approves and holds.
    invoices.append(
        _demo_goods_invoice(
            existing=existing,
            organization=organization,
            supplier=grocery,
            branch=branch,
            item=rice,
            recorder=recorder,
            approver=approver,
            invoiced_on=invoiced_on,
        )
    )

    # 3. A draft carrying freight and a discount, so the allocation is visible.
    invoices.append(
        _demo_draft_invoice(
            existing=existing,
            supplier=chicken_supplier or grocery,
            branch=branch,
            recorder=recorder,
            account=freight_account,
            cost_center=cost_center,
            invoiced_on=invoiced_on,
        )
    )

    # 4. Posted then reversed, so the payable visibly returns to nothing.
    invoices.append(
        _demo_reversed_invoice(
            existing=existing,
            supplier=grocery,
            branch=branch,
            recorder=recorder,
            approver=approver,
            account=freight_account,
            cost_center=cost_center,
            invoiced_on=invoiced_on,
        )
    )
    return [invoice for invoice in invoices if invoice is not None]


def _demo_expense_invoice(
    *,
    existing: dict[str, SupplierInvoice],
    supplier: Supplier,
    branch: Branch,
    recorder: User,
    approver: User,
    account: Account,
    cost_center: CostCenter,
    invoiced_on: datetime.date,
) -> SupplierInvoice:
    reference = "DEMO-SINV-EXPENSE"
    invoice = existing.get(reference)
    if invoice is None:
        invoice = create_supplier_invoice(
            supplier=supplier,
            branch=branch,
            created_by=recorder,
            supplier_invoice_number=reference,
            invoice_date=invoiced_on,
            business_date=invoiced_on,
            supplier_reference="أجور توصيل الشهر",
            notes="نقل بضاعة الشهر — لا يدخل المخزون",
        )
        add_account_line(
            invoice=invoice,
            account=account,
            cost_center=cost_center,
            description="أجور نقل",
            quantity=Decimal("1.000"),
            unit_price=Decimal("75000.000000"),
        )
    return _advance_demo_invoice(invoice, approver=approver, to_status=SupplierInvoiceStatus.POSTED)


def _demo_goods_invoice(
    *,
    existing: dict[str, SupplierInvoice],
    organization: Organization,
    supplier: Supplier,
    branch: Branch,
    item: InventoryItem,
    recorder: User,
    approver: User,
    invoiced_on: datetime.date,
) -> SupplierInvoice:
    reference = "DEMO-SINV-GOODS"
    invoice = existing.get(reference)
    if invoice is None:
        invoice = create_supplier_invoice(
            supplier=supplier,
            branch=branch,
            created_by=recorder,
            supplier_invoice_number=reference,
            invoice_date=invoiced_on,
            business_date=invoiced_on,
            supplier_reference="فاتورة الرز المسلّم",
            notes="مقابل التسليم الجزئي الأول",
        )
        receipt_line = (
            GoodsReceiptLine.objects.filter(
                receipt__organization=organization,
                receipt__supplier=supplier,
                receipt__status=GoodsReceiptStatus.POSTED,
                item=item,
            )
            .order_by("id")
            .first()
        )
        add_inventory_line(
            invoice=invoice,
            item=item,
            base_quantity=Decimal("60.000"),
            # Deliberately a hair above the 1,400 the receipt posted at, so the
            # difference the three-way match exists to surface is actually
            # there to be found in Task 2.11.
            unit_price=Decimal("1450.000000"),
            receipt_line=receipt_line,
            description="رز — فاتورة المورد",
        )
    # Approves and stops. `post_supplier_invoice` would refuse it with
    # `invoice_awaiting_matching`, which is the state this row is here to show.
    return _advance_demo_invoice(
        invoice, approver=approver, to_status=SupplierInvoiceStatus.APPROVED
    )


def _demo_draft_invoice(
    *,
    existing: dict[str, SupplierInvoice],
    supplier: Supplier,
    branch: Branch,
    recorder: User,
    account: Account,
    cost_center: CostCenter,
    invoiced_on: datetime.date,
) -> SupplierInvoice:
    reference = "DEMO-SINV-DRAFT"
    invoice = existing.get(reference)
    if invoice is not None:
        return invoice
    invoice = create_supplier_invoice(
        supplier=supplier,
        branch=branch,
        created_by=recorder,
        supplier_invoice_number=reference,
        invoice_date=invoiced_on,
        business_date=invoiced_on,
        supplier_reference="مسودة للمراجعة",
        freight_amount=Decimal("10000.000"),
        discount_amount=Decimal("3000.000"),
        notes="مسودة: النقل والخصم موزَّعان على الأسطر بطريقة أكبر باقٍ",
    )
    for label, price in (("خدمة تنظيف", "40000.000000"), ("صيانة ثلاجة", "60000.000000")):
        add_account_line(
            invoice=invoice,
            account=account,
            cost_center=cost_center,
            description=label,
            quantity=Decimal("1.000"),
            unit_price=Decimal(price),
        )
    return SupplierInvoice.objects.get(pk=invoice.pk)


def _demo_reversed_invoice(
    *,
    existing: dict[str, SupplierInvoice],
    supplier: Supplier,
    branch: Branch,
    recorder: User,
    approver: User,
    account: Account,
    cost_center: CostCenter,
    invoiced_on: datetime.date,
) -> SupplierInvoice:
    reference = "DEMO-SINV-REVERSED"
    invoice = existing.get(reference)
    if invoice is None:
        invoice = create_supplier_invoice(
            supplier=supplier,
            branch=branch,
            created_by=recorder,
            supplier_invoice_number=reference,
            invoice_date=invoiced_on,
            business_date=invoiced_on,
            supplier_reference="فوترة مكررة من المورد",
        )
        add_account_line(
            invoice=invoice,
            account=account,
            cost_center=cost_center,
            description="رسوم إدارية",
            quantity=Decimal("1.000"),
            unit_price=Decimal("25000.000000"),
        )
    invoice = _advance_demo_invoice(
        invoice, approver=approver, to_status=SupplierInvoiceStatus.POSTED
    )
    if invoice.status == SupplierInvoiceStatus.POSTED:
        reverse_supplier_invoice(
            invoice=invoice,
            actor=approver,
            reason="المورد أرسل الفاتورة مرتين",
        )
    return SupplierInvoice.objects.get(pk=invoice.pk)


def _advance_demo_invoice(
    invoice: SupplierInvoice, *, approver: User, to_status: str
) -> SupplierInvoice:
    """
    Walk one demo invoice up to the state it is meant to be in, and no further.

    Idempotent by construction rather than by a flag: each service refuses a
    document already past the transition it performs, so a second seed run
    finds every invoice where it left it and does nothing.
    """
    if invoice.status == SupplierInvoiceStatus.DRAFT:
        approve_supplier_invoice(invoice=invoice, actor=approver)
        invoice = SupplierInvoice.objects.get(pk=invoice.pk)
    if to_status == SupplierInvoiceStatus.POSTED and invoice.status == (
        SupplierInvoiceStatus.APPROVED
    ):
        post_supplier_invoice(invoice=invoice, actor=approver)
        invoice = SupplierInvoice.objects.get(pk=invoice.pk)
    return invoice


def seed_demo_matches(*, organization: Organization, matcher: User) -> list[PurchaseMatch]:
    """
    Two matches against the rice invoice, and the four queue states around them.

    1. A first attempt, **cancelled** with a reason. It exists so the history
       shows an answer somebody withdrew, and so the release of the quantity it
       was holding is visible: a cancelled match consumes nothing, which is the
       whole reason availability is derived rather than stored.
    2. The replacement, allocated in full and **READY**, carrying a real
       positive price variance — the invoice bills 1,450 a kilogram against the
       1,400 the receipt posted, so the difference three-way matching exists to
       surface is genuinely there rather than contrived to zero.

    Both live on the same invoice, which is exactly what the one-active-match
    constraint permits: it excludes cancelled rows, because a withdrawn answer
    is history rather than a competing claim.

    **Nothing is posted.** The invoice stays `APPROVED`, no journal exists, and
    Task 2.12 is what changes that. The screens say so in as many words.

    What is deliberately left alone gives the queue its other rows: the chicken
    and meat deliveries stay posted with nothing billed against them, and
    `DEMO-SINV-DRAFT` stays a draft with no delivery behind it.

    Idempotent per match, keyed on the note. A second run finds both and does
    nothing.
    """
    existing = {
        row.notes: row
        for row in PurchaseMatch.objects.filter(
            organization=organization, notes__startswith="DEMO-MATCH"
        ).order_by("id")
    }

    goods_invoice = SupplierInvoice.objects.filter(
        organization=organization,
        supplier_invoice_number="DEMO-SINV-GOODS",
        status=SupplierInvoiceStatus.APPROVED,
    ).first()
    if goods_invoice is None:
        return []

    invoice_line = (
        goods_invoice.lines.filter(line_type=SupplierInvoiceLineType.INVENTORY)
        .order_by("sequence")
        .first()
    )
    if invoice_line is None:
        return []
    receipt_line = (
        GoodsReceiptLine.objects.filter(
            receipt__organization=organization,
            receipt__supplier=goods_invoice.supplier,
            receipt__status=GoodsReceiptStatus.POSTED,
            item=invoice_line.item,
        )
        .order_by("id")
        .first()
    )
    if receipt_line is None:
        return []

    matches: list[PurchaseMatch] = []

    # 1. The withdrawn attempt. Allocated, then cancelled with a reason, so
    # both the history and the release of held quantity are visible.
    withdrawn = existing.get("DEMO-MATCH-CANCELLED")
    if withdrawn is None:
        withdrawn = create_purchase_match(
            invoice=goods_invoice, created_by=matcher, notes="DEMO-MATCH-CANCELLED"
        )
        add_allocation(
            match=withdrawn,
            invoice_line=invoice_line,
            receipt_line=receipt_line,
            matched_base_quantity=Decimal("20.000"),
            created_by=matcher,
        )
        cancel_purchase_match(
            match=withdrawn,
            actor=matcher,
            reason="خُصّص السطر الخطأ — أُلغيت وأُعيدت المطابقة",
        )
        withdrawn = PurchaseMatch.objects.get(pk=withdrawn.pk)
    matches.append(withdrawn)

    # 2. The replacement: allocated in full, frozen, and posting nothing.
    agreed = existing.get("DEMO-MATCH-FULL")
    if agreed is None:
        agreed = create_purchase_match(
            invoice=goods_invoice, created_by=matcher, notes="DEMO-MATCH-FULL"
        )
        add_allocation(
            match=agreed,
            invoice_line=invoice_line,
            receipt_line=receipt_line,
            matched_base_quantity=invoice_line.base_quantity or Decimal("0.000"),
            created_by=matcher,
        )
        mark_match_ready(match=agreed, actor=matcher)
        agreed = PurchaseMatch.objects.get(pk=agreed.pk)
    matches.append(agreed)

    # 3. And post it (Task 2.12). The whole reason the demo carries a real
    # price difference rather than a contrived zero: this puts 84,000 back out
    # of GRNI, 87,000 into the payable, and the 3,000 between them into the
    # purchase price variance clearing account, where it is expected to sit
    # until a later period-end process decides how much of it belongs to stock
    # still on hand.
    #
    # Idempotent without a flag: `post_supplier_invoice` refuses an invoice
    # that is already posted, so a second seed run finds it POSTED and skips.
    goods_invoice.refresh_from_db()
    if goods_invoice.status == SupplierInvoiceStatus.APPROVED:
        post_supplier_invoice(invoice=goods_invoice, actor=matcher)

    return matches


def seed_demo_returns(
    *, organization: Organization, storekeeper: User, manager: User
) -> list[SupplierReturn]:
    """
    Three returns, one per lifecycle state (Task 2.13).

    1. `DEMO-SRET-CHICKEN` — **POSTED**. Twenty of the ninety kilograms that
       passed inspection on the warm-chicken delivery spoiled on the shelf and
       went back. The same delivery already had thirty kilograms rejected *at
       the gate*, so the two mechanisms sit side by side on one document
       trail: rejection never entered stock and produced no accounting;
       the return leaves stock at the standing average and parks the claim in
       the supplier-return clearing account.
    2. `DEMO-SRET-DRAFT` — **DRAFT**, five kilograms of rice against the
       matched delivery. A reader needs one return they can post by hand, and
       this one doubles as proof that a live match and a live invoice posting
       do not make a delivery unreturnable — the return takes goods, not the
       payable.
    3. `DEMO-SRET-REVERSED` — two kilograms of meat, posted and then
       **reversed**, so a reader can see that the reversal restores the exact
       value the movement removed rather than today's average, and that the
       delivery becomes returnable again.

    The expected-credit figures are quoted at the *receipt* price on purpose:
    the book value that leaves is the standing moving average, and the gap
    between the two is the difference ADR-022 §2 defers to the credit note.

    Everything goes through the real services. Idempotent per return, keyed on
    the evidence reference: a second run finds each and does nothing further.
    """
    existing = {
        row.evidence_reference: row
        for row in SupplierReturn.objects.filter(
            organization=organization, evidence_reference__startswith="DEMO-SRET"
        ).order_by("id")
    }

    reason = InventoryReasonCode.objects.filter(organization=organization, is_active=True).first()
    returns: list[SupplierReturn] = []

    def _receipt(reference: str) -> GoodsReceipt | None:
        return GoodsReceipt.objects.filter(
            organization=organization,
            delivery_reference=reference,
            status=GoodsReceiptStatus.POSTED,
        ).first()

    def _line(receipt: GoodsReceipt) -> GoodsReceiptLine | None:
        return (
            receipt.lines.filter(accepted_base_quantity__gt=Decimal("0.000"))
            .order_by("sequence")
            .first()
        )

    # 1. The chicken that spoiled after inspection accepted it: POSTED.
    posted = existing.get("DEMO-SRET-CHICKEN")
    if posted is None:
        receipt = _receipt("DEMO-GRN-REJECT")
        line = _line(receipt) if receipt is not None else None
        if receipt is not None and line is not None:
            posted = create_supplier_return(
                receipt=receipt,
                created_by=storekeeper,
                returned_at=receipt.received_at + datetime.timedelta(days=3),
                reason_code=reason,
                reason="تلف بعد القبول — عشرون كيلوغراماً فسدت على الرف",
                evidence_reference="DEMO-SRET-CHICKEN",
            )
            add_return_line(
                supplier_return=posted,
                receipt_line=line,
                returned_base_quantity=Decimal("20.000"),
                # 20 kg at the receipt's 1,400/kg (the 14,000 was per ten-kilo
                # carton). Metadata for the claim; it posts nothing.
                expected_credit_value=Decimal("28000.000"),
                note="بسعر الاستلام ١٬٤٠٠ للكيلوغرام للمطالبة",
            )
    if posted is not None:
        if posted.status == SupplierReturnStatus.DRAFT:
            post_supplier_return(supplier_return=posted, actor=storekeeper)
            posted.refresh_from_db()
        returns.append(posted)

    # 2. Rice against the matched delivery: DRAFT, left for the reader.
    draft = existing.get("DEMO-SRET-DRAFT")
    if draft is None:
        receipt = _receipt("DEMO-GRN-MATCHED")
        line = _line(receipt) if receipt is not None else None
        if receipt is not None and line is not None:
            draft = create_supplier_return(
                receipt=receipt,
                created_by=storekeeper,
                returned_at=receipt.received_at + datetime.timedelta(days=5),
                reason_code=reason,
                reason="أكياس متضررة اكتُشفت عند الفتح",
                evidence_reference="DEMO-SRET-DRAFT",
            )
            add_return_line(
                supplier_return=draft,
                receipt_line=line,
                returned_base_quantity=Decimal("5.000"),
                expected_credit_value=Decimal("7000.000"),
            )
    if draft is not None:
        returns.append(draft)

    # 3. Meat, posted then reversed: the mirror, visible.
    undone = existing.get("DEMO-SRET-REVERSED")
    if undone is None:
        receipt = _receipt("DEMO-GRN-WEIGHED")
        line = _line(receipt) if receipt is not None else None
        if receipt is not None and line is not None:
            undone = create_supplier_return(
                receipt=receipt,
                created_by=storekeeper,
                returned_at=receipt.received_at + datetime.timedelta(days=2),
                reason_code=reason,
                reason="اشتُبه بكيلوغرامين ثم تبيّن سلامتهما",
                evidence_reference="DEMO-SRET-REVERSED",
            )
            add_return_line(
                supplier_return=undone,
                receipt_line=line,
                returned_base_quantity=Decimal("2.000"),
                expected_credit_value=Decimal("19000.000"),
            )
    if undone is not None:
        if undone.status == SupplierReturnStatus.DRAFT:
            post_supplier_return(supplier_return=undone, actor=storekeeper)
            undone.refresh_from_db()
        if undone.status == SupplierReturnStatus.POSTED:
            # Reversal is the manager's act: the storekeeper role can record
            # and post a return but deliberately cannot unwind one.
            reverse_supplier_return(
                supplier_return=undone,
                actor=manager,
                reason="فحص المورد أكّد سلامة الكمية — أُعيدت إلى المخزن",
            )
            undone.refresh_from_db()
        returns.append(undone)

    return returns


def seed_demo_credit_notes(
    *, organization: Organization, recorder: User, poster: User
) -> list[SupplierCreditNote]:
    """
    One credit note: the supplier's answer to the chicken return (Task 2.14).

    `DEMO-SCN-CHICKEN` credits the 28,000 the goods were bought for — twenty
    kilograms at the receipt's 1,400 — against a book value of 40,514.706,
    because the standing average had blended dearer earlier stock. Posting it
    closes the chicken claim in `8-01-04-001`, debits the payable 28,000, and
    recognises the 12,514.706 difference in `7-09-04-001` — the ADR-022 §2 gap
    landing on paper, as a loss the buyer can see.

    No allocation, deliberately: the chicken delivery has no posted invoice in
    this dataset, so the note stands as **unallocated supplier credit** —
    PRC-051's second outcome, visible rather than merely specified. The other
    two demo returns cannot take a note (one is a draft, one is reversed),
    which the screens explain by omission.

    Everything through the real services; idempotent by the supplier document
    number. `recorder` and `poster` differ because the maker-checker split is
    real: whoever agrees a credit is real should not have typed it in.
    """
    existing = {
        row.supplier_document_number: row
        for row in SupplierCreditNote.objects.filter(
            organization=organization, supplier_document_number__startswith="DEMO-SCN"
        ).order_by("id")
    }
    notes: list[SupplierCreditNote] = []

    note = existing.get("DEMO-SCN-CHICKEN")
    if note is None:
        settled_return = SupplierReturn.objects.filter(
            organization=organization,
            evidence_reference="DEMO-SRET-CHICKEN",
            status=SupplierReturnStatus.POSTED,
        ).first()
        if settled_return is None:
            return []
        note = create_supplier_credit_note(
            supplier_return=settled_return,
            created_by=recorder,
            supplier_document_number="DEMO-SCN-CHICKEN",
            credit_date=settled_return.returned_at + datetime.timedelta(days=7),
            amount=Decimal("28000.000"),
            reason="اعتماد المورد لمرتجع الدجاج بسعر الشراء",
        )
        # The settlement is explicit and per line: the whole twenty kilograms,
        # so this note takes the line's exact remaining book value.
        add_return_allocation(
            credit_note=note,
            return_line=settled_return.lines.get(),
            credited_base_quantity=Decimal("20.000"),
            allocated_credit_amount=Decimal("28000.000"),
        )
    if note.status == SupplierCreditNoteStatus.DRAFT:
        if not note.return_allocations.exists():
            add_return_allocation(
                credit_note=note,
                return_line=note.supplier_return.lines.get(),
                credited_base_quantity=Decimal("20.000"),
                allocated_credit_amount=Decimal("28000.000"),
            )
        post_supplier_credit_note(credit_note=note, actor=poster)
        note.refresh_from_db()
    notes.append(note)
    return notes


def seed_demo_payments(
    *, organization: Organization, recorder: User, poster: User
) -> list[SupplierPayment]:
    """
    One payment: sixty thousand to the grocer against the rice bill (Task 2.15).

    `DEMO-SPAY-GOODS` allocates 50,000 of its 60,000 against the posted
    87,000 rice invoice and leaves 10,000 unallocated — so the screen shows
    both halves of the journal at once: the payable falling by the allocated
    share and a real supplier advance standing as an asset (PRC-055), not a
    negative payable. Paid by bank, so the source account arrives through
    `SUPPLIER_PAYMENT_BANK` (PRC-056) rather than any account named here.

    Everything through the real services; idempotent by reference. The
    recorder and poster differ because letting money go is the checker's act.
    """
    existing = {
        row.reference: row
        for row in SupplierPayment.objects.filter(
            organization=organization, reference__startswith="DEMO-SPAY"
        ).order_by("id")
    }
    payments: list[SupplierPayment] = []

    payment = existing.get("DEMO-SPAY-GOODS")
    if payment is None:
        invoice = SupplierInvoice.objects.filter(
            organization=organization,
            supplier_invoice_number="DEMO-SINV-GOODS",
            status=SupplierInvoiceStatus.POSTED,
        ).first()
        branch = Branch.objects.filter(organization=organization, code="DEMO-BUNOOK").first()
        if invoice is None or branch is None:
            return []
        payment = create_supplier_payment(
            supplier=invoice.supplier,
            branch=branch,
            created_by=recorder,
            paid_at=invoice.invoice_date + datetime.timedelta(days=10),
            method="BANK",
            amount=Decimal("60000.000"),
            reference="DEMO-SPAY-GOODS",
            notes="دفعة على فاتورة الرز مع سلفة",
        )
        add_payment_allocation(
            payment=payment,
            invoice=invoice,
            allocated_amount=Decimal("50000.000"),
        )
    if payment.status == SupplierPaymentStatus.DRAFT:
        post_supplier_payment(payment=payment, actor=poster)
        payment.refresh_from_db()
    payments.append(payment)
    return payments
