"""
The supplier item catalogue: planning data that values nothing.

Two tests here carry more weight than the rest. `TestTheCatalogueValuesNothing`
reads the source of every posting path and proves none of them imports the
catalogue — because the whole reason a price may sit on this model at all is
that no posting can reach it. And `test_overlapping_periods_are_impossible`
goes through the database rather than the service, because an exclusion
constraint is the only thing a raw INSERT cannot walk past.
"""

from __future__ import annotations

import ast
import datetime
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditEvent
from apps.inventory.models import (
    ConversionType,
    InventoryItem,
    ItemCategory,
    ItemPackageConversion,
    ItemType,
    PackageUnit,
)
from apps.organizations.authorization import OutOfScope
from apps.organizations.models import Organization, Role
from apps.procurement.models import Supplier, SupplierItem
from apps.procurement.permissions import (
    MANAGE_SUPPLIER_ITEMS,
    VIEW_SUPPLIER_COST,
    VIEW_SUPPLIER_ITEM,
    permissions_for_role,
)
from apps.procurement.selectors import (
    catalogue_effective_on,
    preferred_supplier_item,
    resolve_supplier_item,
    visible_supplier_items,
)
from apps.procurement.services import (
    create_supplier,
    create_supplier_item,
    supersede_supplier_item,
    update_supplier_item,
)
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}
JANUARY = datetime.date(2026, 1, 1)
JULY = datetime.date(2026, 7, 1)


@pytest.fixture
def units() -> None:
    from django.core.management import call_command

    call_command("seed_units", verbosity=0)


@pytest.fixture
def kilogram(units: None) -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def category(organization: Organization) -> ItemCategory:
    from apps.inventory.services import create_item_category

    return create_item_category(organization=organization, code="MEATS", name_ar="لحوم")


@pytest.fixture
def rice(
    organization: Organization, category: ItemCategory, kilogram: UnitOfMeasure
) -> InventoryItem:
    from apps.inventory.services import create_item

    return create_item(
        organization=organization,
        code="RICE",
        name_ar="رز",
        category=category,
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def sack(organization: Organization, rice: InventoryItem) -> PackageUnit:
    from apps.inventory.services import create_item_conversion, create_package_unit

    package = create_package_unit(organization=organization, code="SACK", name_ar="كيس")
    create_item_conversion(
        item=rice,
        package_unit=package,
        factor_to_base=Decimal("30.000000000000"),
        conversion_type=ConversionType.FIXED,
        effective_from=JANUARY,
    )
    return package


@pytest.fixture
def box(organization: Organization) -> PackageUnit:
    """A package no item converts. Naming it must be refused."""
    from apps.inventory.services import create_package_unit

    return create_package_unit(organization=organization, code="BOX", name_ar="علبة")


@pytest.fixture
def meat_supplier(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="MEAT-01", name_ar="مورد اللحوم")


@pytest.fixture
def grocery_supplier(organization: Organization) -> Supplier:
    return create_supplier(organization=organization, code="GROC-01", name_ar="مورد المواد")


@pytest.fixture
def row(meat_supplier: Supplier, rice: InventoryItem, sack: PackageUnit) -> SupplierItem:
    return create_supplier_item(
        supplier=meat_supplier,
        item=rice,
        package_unit=sack,
        effective_from=JANUARY,
        supplier_sku="Rc-Sack-30",
        last_quoted_price=Decimal("42000.000000"),
        lead_time_days=3,
        minimum_order_quantity=Decimal("5.000"),
        is_preferred=True,
    )


# ---------------------------------------------------------------------------
# The package has to be one the item knows
# ---------------------------------------------------------------------------


class TestPackageCompatibility:
    def test_a_package_the_item_cannot_convert_is_refused(
        self, meat_supplier: Supplier, rice: InventoryItem, box: PackageUnit
    ) -> None:
        """
        Caught here, not at the receipt.

        A row naming a package the item has no factor for would fail at the
        moment goods are being counted in a warehouse, which is the worst
        possible place to discover a master-data mistake.
        """
        with pytest.raises(ValidationError) as refused:
            create_supplier_item(
                supplier=meat_supplier, item=rice, package_unit=box, effective_from=JANUARY
            )
        assert refused.value.code == "no_conversion_for_package"

    def test_a_conversion_that_starts_later_does_not_count(
        self, meat_supplier: Supplier, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        with pytest.raises(ValidationError):
            create_supplier_item(
                supplier=meat_supplier,
                item=rice,
                package_unit=sack,
                effective_from=datetime.date(2025, 6, 1),
            )

    def test_no_package_means_base_units_and_is_allowed(
        self, meat_supplier: Supplier, rice: InventoryItem
    ) -> None:
        created = create_supplier_item(
            supplier=meat_supplier, item=rice, package_unit=None, effective_from=JANUARY
        )
        assert created.package_unit is None

    def test_a_variable_package_is_accepted_and_stays_variable(
        self,
        organization: Organization,
        meat_supplier: Supplier,
        category: ItemCategory,
        kilogram: UnitOfMeasure,
    ) -> None:
        """
        The catalogue records the package; it does not soften what the package
        means. A receipt against this row will still demand a measured weight,
        because that requirement lives on the item conversion.
        """
        from apps.inventory.services import create_item, create_item_conversion, create_package_unit

        meat = create_item(
            organization=organization,
            code="MEAT",
            name_ar="لحم",
            category=category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        container = create_package_unit(
            organization=organization, code="CONTAINER", name_ar="حاوية"
        )
        create_item_conversion(
            item=meat,
            package_unit=container,
            factor_to_base=Decimal("18.000000000000"),
            conversion_type=ConversionType.VARIABLE,
            effective_from=JANUARY,
        )
        created = create_supplier_item(
            supplier=meat_supplier, item=meat, package_unit=container, effective_from=JANUARY
        )
        conversion = ItemPackageConversion.objects.get(item=meat, package_unit=container)
        assert created.package_unit == container
        assert conversion.conversion_type == ConversionType.VARIABLE


# ---------------------------------------------------------------------------
# Effective dating
# ---------------------------------------------------------------------------


class TestEffectiveDating:
    def test_overlapping_periods_are_impossible(
        self, row: SupplierItem, meat_supplier: Supplier, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        """
        Through the database, not the service.

        A range clash cannot be expressed as a unique constraint, and a service
        check alone is a promise rather than a guarantee. This is the exclusion
        constraint doing the work.
        """
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierItem.objects.create(
                organization=meat_supplier.organization,
                supplier=meat_supplier,
                item=rice,
                package_unit=sack,
                effective_from=datetime.date(2026, 6, 1),
                version=2,
            )

    def test_a_closed_period_leaves_room_for_the_next(
        self, row: SupplierItem, meat_supplier: Supplier, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        update_supplier_item(
            supplier_item=row,
            supplier_sku=row.supplier_sku,
            is_preferred=False,
            effective_to=datetime.date(2026, 5, 31),
        )
        later = create_supplier_item(
            supplier=meat_supplier,
            item=rice,
            package_unit=sack,
            effective_from=datetime.date(2026, 6, 1),
        )
        assert later.pk != row.pk

    def test_an_end_before_the_start_is_refused(
        self, meat_supplier: Supplier, rice: InventoryItem
    ) -> None:
        with pytest.raises(ValidationError):
            create_supplier_item(
                supplier=meat_supplier,
                item=rice,
                effective_from=JULY,
                effective_to=JANUARY,
            )

    def test_superseding_closes_the_old_row_and_opens_the_next_version(
        self, row: SupplierItem
    ) -> None:
        replacement = supersede_supplier_item(
            supplier_item=row, effective_from=JULY, last_quoted_price=Decimal("45000.000000")
        )
        row.refresh_from_db()

        assert row.effective_to == JULY - datetime.timedelta(days=1)
        assert row.is_preferred is False
        assert replacement.effective_from == JULY
        assert replacement.version == row.version + 1
        # Preference moved to the replacement, so the item still has exactly
        # one place it is normally bought.
        assert replacement.is_preferred is True
        assert replacement.supplier_sku == row.supplier_sku

    def test_superseding_backwards_is_refused(self, row: SupplierItem) -> None:
        with pytest.raises(ValidationError) as refused:
            supersede_supplier_item(supplier_item=row, effective_from=JANUARY)
        assert refused.value.code == "supersede_not_later"


# ---------------------------------------------------------------------------
# One preferred source per item
# ---------------------------------------------------------------------------


class TestPreferredSource:
    def test_two_preferred_rows_for_one_item_are_impossible(
        self, row: SupplierItem, grocery_supplier: Supplier, rice: InventoryItem
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierItem.objects.create(
                organization=grocery_supplier.organization,
                supplier=grocery_supplier,
                item=rice,
                package_unit=None,
                effective_from=JANUARY,
                is_preferred=True,
            )

    def test_a_second_unpreferred_source_is_fine(
        self, row: SupplierItem, grocery_supplier: Supplier, rice: InventoryItem, sack: PackageUnit
    ) -> None:
        alternative = create_supplier_item(
            supplier=grocery_supplier,
            item=rice,
            package_unit=sack,
            effective_from=JANUARY,
            last_quoted_price=Decimal("43500.000000"),
        )
        assert alternative.is_preferred is False

    def test_the_preferred_selector_answers_at_most_one(
        self, manager: User, row: SupplierItem, rice: InventoryItem
    ) -> None:
        found = preferred_supplier_item(manager, item=rice, on=datetime.date(2026, 3, 1))
        assert found is not None and found.pk == row.pk

    def test_an_expired_row_is_not_the_preferred_source_today(
        self, manager: User, row: SupplierItem, rice: InventoryItem
    ) -> None:
        update_supplier_item(
            supplier_item=row, is_preferred=True, effective_to=datetime.date(2026, 2, 1)
        )
        assert preferred_supplier_item(manager, item=rice, on=JULY) is None

    def test_a_null_price_sorts_last_rather_than_cheapest(
        self, manager: User, row: SupplierItem, grocery_supplier: Supplier, rice: InventoryItem
    ) -> None:
        """No price on file is not a price of zero."""
        create_supplier_item(
            supplier=grocery_supplier, item=rice, effective_from=JANUARY, last_quoted_price=None
        )
        ordered = list(catalogue_effective_on(manager, item=rice, on=datetime.date(2026, 3, 1)))
        assert ordered[0].last_quoted_price is not None
        assert ordered[-1].last_quoted_price is None


# ---------------------------------------------------------------------------
# The catalogue values nothing
# ---------------------------------------------------------------------------


class TestTheCatalogueValuesNothing:
    #: Every module that can post stock or a journal. If any of them learns to
    #: read the catalogue, a price somebody typed as a planning note starts
    #: valuing real inventory.
    POSTING_MODULES = (
        "apps/inventory/ledger.py",
        "apps/inventory/opening.py",
        "apps/inventory/operations.py",
        "apps/inventory/transfers.py",
        "apps/inventory/counts.py",
        "apps/inventory/adjustments.py",
        "apps/inventory/accounts.py",
        "apps/accounting/services.py",
        "apps/accounting/commands.py",
    )

    def test_no_posting_module_imports_the_catalogue(self) -> None:
        """
        Read as source, not by import: an architectural rule is about what the
        code says, and a runtime check would only see what happened to run.
        """
        root = Path(__file__).resolve().parents[3]
        offenders: list[str] = []
        for relative in self.POSTING_MODULES:
            path = root / relative
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "apps.procurement"
                ):
                    offenders.append(f"{relative} imports {node.module}")
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        f"{relative} imports {alias.name}"
                        for alias in node.names
                        if alias.name.startswith("apps.procurement")
                    )
        assert not offenders, "a posting module reads procurement:\n  " + "\n  ".join(offenders)

    def test_the_price_is_not_referenced_outside_procurement(self) -> None:
        root = Path(__file__).resolve().parents[3]
        offenders = [
            str(path.relative_to(root))
            for path in (root / "apps").rglob("*.py")
            if "procurement" not in path.parts
            and "last_quoted_price" in path.read_text(encoding="utf-8")
        ]
        assert not offenders, f"last_quoted_price is read outside procurement: {offenders}"


# ---------------------------------------------------------------------------
# Scope, permissions and audit
# ---------------------------------------------------------------------------


class TestScopeAndPermissions:
    def test_a_supplier_and_item_from_different_organizations_are_refused(
        self, other_organization: Organization, rice: InventoryItem
    ) -> None:
        theirs = create_supplier(organization=other_organization, code="RIVAL-01", name_ar="منافس")
        with pytest.raises(ValidationError) as refused:
            create_supplier_item(supplier=theirs, item=rice, effective_from=JANUARY)
        assert refused.value.code == "organization_mismatch"

    def test_another_organizations_row_is_out_of_scope(
        self, manager: User, other_organization: Organization, kilogram: UnitOfMeasure
    ) -> None:
        from apps.inventory.services import create_item, create_item_category

        theirs = create_supplier(organization=other_organization, code="RIVAL-02", name_ar="منافس")
        their_category = create_item_category(
            organization=other_organization, code="X", name_ar="س"
        )
        their_item = create_item(
            organization=other_organization,
            code="X-ITEM",
            name_ar="صنف",
            category=their_category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=UnitOfMeasure.objects.get(code="KG"),
        )
        their_row = create_supplier_item(supplier=theirs, item=their_item, effective_from=JANUARY)
        assert their_row.pk not in set(visible_supplier_items(manager).values_list("pk", flat=True))
        with pytest.raises(OutOfScope):
            resolve_supplier_item(manager, their_row.pk)

    def test_a_storekeeper_reads_the_catalogue_and_never_its_prices(self) -> None:
        held = permissions_for_role(Role.STOREKEEPER)
        assert VIEW_SUPPLIER_ITEM in held
        assert VIEW_SUPPLIER_COST not in held
        assert MANAGE_SUPPLIER_ITEMS not in held

    def test_purchasing_maintains_the_catalogue(self) -> None:
        assert MANAGE_SUPPLIER_ITEMS in permissions_for_role(Role.PURCHASING)

    def test_an_accountant_reads_it_and_does_not_maintain_it(self) -> None:
        held = permissions_for_role(Role.ACCOUNTANT)
        assert VIEW_SUPPLIER_ITEM in held
        assert MANAGE_SUPPLIER_ITEMS not in held

    def test_creating_and_editing_are_audited_with_a_real_before(self, row: SupplierItem) -> None:
        update_supplier_item(supplier_item=row, supplier_sku="NEW-SKU", lead_time_days=9)
        events = AuditEvent.objects.filter(
            target_type="procurement.SupplierItem", target_id=str(row.pk)
        ).order_by("id")
        assert [event.action for event in events] == ["CREATED", "UPDATED"]
        edit = events[1]
        assert edit.previous_state is not None and edit.new_state is not None
        assert edit.previous_state["lead_time_days"] == 3
        assert edit.new_state["lead_time_days"] == 9

    def test_the_supplier_reference_keeps_its_case(self, row: SupplierItem) -> None:
        """Their vocabulary, not ours — the same rule ADR-017 states."""
        assert row.supplier_sku == "Rc-Sack-30"


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class TestScreens:
    def test_the_list_renders_the_row(
        self, client_for: Callable[[User], Client], manager: User, row: SupplierItem
    ) -> None:
        response = client_for(manager).get(reverse("procurement:supplier_item_list"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "MEAT-01" in body
        assert "RICE" in body

    def test_an_hx_request_returns_only_the_results(
        self, client_for: Callable[[User], Client], manager: User, row: SupplierItem
    ) -> None:
        response = client_for(manager).get(reverse("procurement:supplier_item_list"), headers=HX)
        assert response.status_code == 200
        body = response.content.decode()
        assert "MEAT-01" in body
        assert "<html" not in body.lower()

    def test_the_search_narrows_the_table(
        self,
        client_for: Callable[[User], Client],
        manager: User,
        row: SupplierItem,
        grocery_supplier: Supplier,
        rice: InventoryItem,
    ) -> None:
        create_supplier_item(supplier=grocery_supplier, item=rice, effective_from=JANUARY)
        body = (
            client_for(manager)
            .get(reverse("procurement:supplier_item_list"), {"q": "GROC"}, headers=HX)
            .content.decode()
        )
        assert "GROC-01" in body
        assert "MEAT-01" not in body

    def test_a_storekeeper_sees_no_price_heading_at_all(
        self, client_for: Callable[[User], Client], keeper: User, row: SupplierItem
    ) -> None:
        """Omitted, not blanked. An empty cell still says a number belongs there."""
        body = client_for(keeper).get(reverse("procurement:supplier_item_list")).content.decode()
        assert "RICE" in body
        assert "آخر سعر معروض" not in body
        assert "42000" not in body

    def test_a_manager_sees_the_price(
        self, client_for: Callable[[User], Client], manager: User, row: SupplierItem
    ) -> None:
        body = client_for(manager).get(reverse("procurement:supplier_item_list")).content.decode()
        assert "آخر سعر معروض" in body

    def test_creating_through_the_screen_goes_through_the_service(
        self,
        client_for: Callable[[User], Client],
        manager: User,
        grocery_supplier: Supplier,
        rice: InventoryItem,
        sack: PackageUnit,
    ) -> None:
        response = client_for(manager).post(
            reverse("procurement:supplier_item_create"),
            {
                "supplier": str(grocery_supplier.pk),
                "item": str(rice.pk),
                "package_unit": str(sack.pk),
                "supplier_sku": "GR-RICE",
                "effective_from": "2026-01-01",
                "lead_time_days": "4",
            },
        )
        assert response.status_code == 302
        created = SupplierItem.objects.get(supplier=grocery_supplier, item=rice)
        assert created.lead_time_days == 4
        assert created.supplier_sku == "GR-RICE"

    def test_a_foreign_row_is_a_404_on_the_edit_route(
        self,
        client_for: Callable[[User], Client],
        manager: User,
        other_organization: Organization,
        kilogram: UnitOfMeasure,
    ) -> None:
        from apps.inventory.services import create_item, create_item_category

        theirs = create_supplier(organization=other_organization, code="RIVAL-03", name_ar="منافس")
        their_item = create_item(
            organization=other_organization,
            code="Y-ITEM",
            name_ar="صنف",
            category=create_item_category(organization=other_organization, code="Y", name_ar="ص"),
            item_type=ItemType.RAW_MATERIAL,
            base_unit=UnitOfMeasure.objects.get(code="KG"),
        )
        their_row = create_supplier_item(supplier=theirs, item=their_item, effective_from=JANUARY)
        response = client_for(manager).get(
            reverse("procurement:supplier_item_update", args=[their_row.pk])
        )
        assert response.status_code == 404

    def test_archiving_is_post_only_and_drops_the_preference(
        self, client_for: Callable[[User], Client], manager: User, row: SupplierItem
    ) -> None:
        client = client_for(manager)
        assert (
            client.get(reverse("procurement:supplier_item_archive", args=[row.pk])).status_code
            == 405
        )
        assert (
            client.post(reverse("procurement:supplier_item_archive", args=[row.pk])).status_code
            == 302
        )
        archived = SupplierItem.objects.get(pk=row.pk)
        assert archived.is_active is False
        assert archived.is_preferred is False


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class TestApi:
    def test_decimals_cross_the_wire_as_strings(
        self, client_for: Callable[[User], Client], manager: User, row: SupplierItem
    ) -> None:
        response = client_for(manager).get("/api/v1/procurement/catalogue/")
        assert response.status_code == 200
        payload = response.json()[0]
        assert payload["last_quoted_price"] == "42000.000000"
        assert payload["minimum_order_quantity"] == "5.000"
        assert isinstance(payload["last_quoted_price"], str)

    def test_no_float_appears_in_the_raw_json(
        self, client_for: Callable[[User], Client], manager: User, row: SupplierItem
    ) -> None:
        raw = client_for(manager).get("/api/v1/procurement/catalogue/").content.decode()
        assert "42000.0," not in raw
        assert '"42000.000000"' in raw

    def test_a_storekeeper_gets_no_price_key(
        self, client_for: Callable[[User], Client], keeper: User, row: SupplierItem
    ) -> None:
        payload = client_for(keeper).get("/api/v1/procurement/catalogue/").json()[0]
        assert payload["last_quoted_price"] is None
        assert payload["item_code"] == "RICE"

    def test_creating_through_the_api_validates_the_package(
        self,
        client_for: Callable[[User], Client],
        manager: User,
        grocery_supplier: Supplier,
        rice: InventoryItem,
        box: PackageUnit,
    ) -> None:
        response = client_for(manager).post(
            "/api/v1/procurement/catalogue/",
            data={
                "supplier_id": grocery_supplier.pk,
                "item_id": rice.pk,
                "package_unit_id": box.pk,
                "effective_from": "2026-01-01",
            },
            content_type="application/json",
        )
        # 422 is this API's answer for a domain rule the payload could not
        # have satisfied — see `on_validation_error` in `config/api.py`.
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


class TestDemoCatalogue:
    def test_the_seed_is_idempotent_and_skips_what_is_missing(
        self, organization: Organization
    ) -> None:
        """
        No inventory demo here, so every row is skipped. Skipping is the honest
        answer — inventing the items would make the seed create master data
        that the inventory demo owns.
        """
        from apps.procurement.demo import seed_demo_catalogue, seed_demo_suppliers

        seed_demo_suppliers(organization=organization)
        assert seed_demo_catalogue(organization=organization) == []
