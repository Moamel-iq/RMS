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

from apps.inventory.models import InventoryItem, PackageUnit, Warehouse
from apps.organizations.models import Branch, Organization
from apps.procurement.comparison import award_quotation
from apps.procurement.models import (
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
    SupplierItem,
    SupplierQuotation,
    SupplierQuotationStatus,
)
from apps.procurement.services import (
    add_quotation_line,
    add_request_line,
    approve_purchase_request,
    create_purchase_request,
    create_supplier,
    create_supplier_item,
    create_supplier_quotation,
    reject_purchase_request,
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
