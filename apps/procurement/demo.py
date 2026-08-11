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
from apps.procurement.models import PurchaseRequest, Supplier, SupplierItem
from apps.procurement.services import (
    add_request_line,
    approve_purchase_request,
    create_purchase_request,
    create_supplier,
    create_supplier_item,
    reject_purchase_request,
    submit_purchase_request,
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
