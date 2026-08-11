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

from apps.organizations.models import Organization
from apps.procurement.models import Supplier
from apps.procurement.services import create_supplier

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
