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

from apps.inventory.models import (
    InventoryItem,
    InventoryLot,
    InventoryReasonCode,
    PackageUnit,
    Warehouse,
)
from apps.organizations.models import Branch, Organization
from apps.procurement.comparison import award_quotation
from apps.procurement.models import (
    GoodsReceipt,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
    SupplierItem,
    SupplierQuotation,
    SupplierQuotationStatus,
)
from apps.procurement.posting import post_goods_receipt, reverse_goods_receipt
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
    Five deliveries, each showing something the others do not.

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
