"""
Task 2.17 — procurement's import kinds on the Task 1.7 framework.

The framework's own guarantees (preview writes nothing, all-or-nothing
apply, content-hash retry guard, file security) are proven in
`apps/inventory/tests`; what these tests prove is procurement's use of it:
every write goes through the real services, references resolve only inside
the batch's own organization, the compound row identities work, the draft
kind produces a reviewable draft and nothing further, and the §16.8
boundary — no import kind for any posted document — holds by vocabulary.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from apps.core.context import audit_context
from apps.inventory.imports import apply_batch, create_batch, validate_batch
from apps.inventory.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportKind,
    InventoryItem,
    ItemType,
    Warehouse,
)
from apps.organizations.models import Branch, Organization
from apps.procurement.models import (
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
    SupplierItem,
)
from apps.procurement.services import create_supplier
from apps.units.models import UnitOfMeasure
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def kilogram() -> UnitOfMeasure:
    call_command("seed_units", verbosity=0)
    return UnitOfMeasure.objects.get(code="KG")


@pytest.fixture
def rice(organization: Organization, kilogram: UnitOfMeasure) -> InventoryItem:
    from apps.inventory.services import create_item, create_item_category

    return create_item(
        organization=organization,
        code="RICE",
        name_ar="رز",
        category=create_item_category(organization=organization, code="GRAINS", name_ar="حبوب"),
        item_type=ItemType.RAW_MATERIAL,
        base_unit=kilogram,
    )


@pytest.fixture
def store(branch: Branch) -> Warehouse:
    from apps.inventory.services import create_warehouse

    return create_warehouse(branch=branch, code="MAIN", name_ar="مخزن")


def _csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _run(  # upload → validate, the shared first half of most tests here
    *,
    organization: Organization,
    kind: str,
    raw: bytes,
    actor: User,
    branch: Branch | None = None,
) -> ImportBatch:
    with audit_context(actor=actor):
        batch = create_batch(
            organization=organization,
            kind=kind,
            raw=raw,
            filename="upload.csv",
            branch=branch,
        )
        return validate_batch(batch=batch)


class TestSupplierImport:
    def test_the_lifecycle_creates_updates_and_leaves_unchanged(
        self, organization: Organization, manager: User
    ) -> None:
        first = _csv(
            "code,name_ar,payment_terms_days",
            "GROC-01,مورد المواد,30",
            "MEAT-01,مورد اللحوم,0",
        )
        batch = _run(organization=organization, kind=ImportKind.SUPPLIER, raw=first, actor=manager)
        assert batch.status == ImportBatchStatus.VALIDATED
        with audit_context(actor=manager):
            applied = apply_batch(batch=batch)
        assert applied.applied_row_count == 2
        grocery = Supplier.objects.get(organization=organization, code="GROC-01")
        assert grocery.name_ar == "مورد المواد"
        assert grocery.payment_terms_days == 30

        # The same content again is a retry, recognised and refused.
        again = _run(organization=organization, kind=ImportKind.SUPPLIER, raw=first, actor=manager)
        with audit_context(actor=manager), pytest.raises(ValidationError) as refusal:
            apply_batch(batch=again)
        assert refusal.value.code == "import_content_already_applied"

        # A changed row updates through the service; an identical one counts
        # as unchanged rather than as a change.
        second = _csv(
            "code,name_ar,payment_terms_days",
            "GROC-01,مورد المواد الغذائية,30",
            "MEAT-01,مورد اللحوم,0",
        )
        revision = _run(
            organization=organization, kind=ImportKind.SUPPLIER, raw=second, actor=manager
        )
        with audit_context(actor=manager):
            applied_revision = apply_batch(batch=revision)
        assert applied_revision.applied_row_count == 1
        actions = {row.external_key: row.applied_action for row in applied_revision.rows.all()}
        assert actions == {"GROC-01": "updated", "MEAT-01": "unchanged"}

    def test_a_missing_arabic_name_fails_the_row_and_blocks_the_apply(
        self, organization: Organization, manager: User
    ) -> None:
        batch = _run(
            organization=organization,
            kind=ImportKind.SUPPLIER,
            raw=_csv("code,name_ar", "GROC-01,مورد", "BAD-01,"),
            actor=manager,
        )
        assert batch.status == ImportBatchStatus.FAILED_VALIDATION
        with audit_context(actor=manager), pytest.raises(ValidationError):
            apply_batch(batch=batch)
        assert Supplier.objects.count() == 0

    def test_the_same_code_twice_in_one_file_poisons_both_rows(
        self, organization: Organization, manager: User
    ) -> None:
        batch = _run(
            organization=organization,
            kind=ImportKind.SUPPLIER,
            raw=_csv("code,name_ar", "GROC-01,مورد", "GROC-01,مورد آخر"),
            actor=manager,
        )
        assert batch.status == ImportBatchStatus.FAILED_VALIDATION
        assert batch.error_row_count == 2


class TestCatalogueImport:
    def test_a_valid_row_reaches_the_catalogue_through_the_service(
        self, organization: Organization, manager: User, rice: InventoryItem
    ) -> None:
        create_supplier(organization=organization, code="GROC-01", name_ar="مورد")
        batch = _run(
            organization=organization,
            kind=ImportKind.SUPPLIER_ITEM,
            raw=_csv(
                "supplier_code,item_code,effective_from,last_quoted_price,is_preferred",
                "GROC-01,RICE,2026-01-01,1400.000000,نعم",
            ),
            actor=manager,
        )
        assert batch.status == ImportBatchStatus.VALIDATED
        with audit_context(actor=manager):
            apply_batch(batch=batch)
        row = SupplierItem.objects.get()
        assert row.supplier.code == "GROC-01"
        assert row.item.code == "RICE"
        assert row.last_quoted_price == Decimal("1400.000000")
        assert row.is_preferred is True

    def test_one_item_from_two_suppliers_is_not_an_in_file_duplicate(
        self, organization: Organization, manager: User, rice: InventoryItem
    ) -> None:
        """The compound identity: supplier *and* item, never item alone."""
        create_supplier(organization=organization, code="GROC-01", name_ar="مورد")
        create_supplier(organization=organization, code="MEAT-01", name_ar="مورد آخر")
        batch = _run(
            organization=organization,
            kind=ImportKind.SUPPLIER_ITEM,
            raw=_csv(
                "supplier_code,item_code,effective_from",
                "GROC-01,RICE,2026-01-01",
                "MEAT-01,RICE,2026-01-01",
            ),
            actor=manager,
        )
        assert batch.status == ImportBatchStatus.VALIDATED

    def test_another_organizations_references_resolve_to_nothing(
        self,
        organization: Organization,
        other_organization: Organization,
        manager: User,
        rice: InventoryItem,
    ) -> None:
        """
        The batch's organization is the world. A supplier and an item that
        exist — in the other organization — fail as unknown, the same answer
        the screen would give and one that reveals nothing about elsewhere.
        """
        create_supplier(organization=other_organization, code="RIVAL-01", name_ar="مورد منافس")
        batch = _run(
            organization=organization,
            kind=ImportKind.SUPPLIER_ITEM,
            raw=_csv(
                "supplier_code,item_code,effective_from",
                "RIVAL-01,RICE,2026-01-01",
            ),
            actor=manager,
        )
        assert batch.status == ImportBatchStatus.FAILED_VALIDATION
        row = batch.rows.get()
        assert "supplier_code" in row.errors


class TestPurchaseRequestDraftImport:
    def test_rows_sharing_the_triple_become_one_reviewable_draft(
        self,
        organization: Organization,
        branch: Branch,
        store: Warehouse,
        manager: User,
        rice: InventoryItem,
        kilogram: UnitOfMeasure,
    ) -> None:
        from apps.inventory.services import create_item, create_item_category

        create_item(
            organization=organization,
            code="OIL",
            name_ar="زيت",
            category=create_item_category(organization=organization, code="OILS", name_ar="زيوت"),
            item_type=ItemType.RAW_MATERIAL,
            base_unit=kilogram,
        )
        batch = _run(
            organization=organization,
            kind=ImportKind.PURCHASE_REQUEST_DRAFT,
            raw=_csv(
                "warehouse_code,required_date,purpose,item_code,quantity",
                "MAIN,2026-09-01,تموين الأسبوع,RICE,50.000",
                "MAIN,2026-09-01,تموين الأسبوع,OIL,10.000",
            ),
            actor=manager,
            branch=branch,
        )
        assert batch.status == ImportBatchStatus.VALIDATED
        with audit_context(actor=manager):
            apply_batch(batch=batch)

        request = PurchaseRequest.objects.get()
        assert request.status == PurchaseRequestStatus.DRAFT
        assert request.number == ""  # no number drawn: a draft, not a document
        assert request.branch == branch
        assert sorted(request.lines.values_list("item__code", flat=True)) == ["OIL", "RICE"]
        line = request.lines.get(item__code="RICE")
        assert line.base_quantity == Decimal("50.000")

    def test_a_different_purpose_is_a_different_draft(
        self,
        organization: Organization,
        branch: Branch,
        store: Warehouse,
        manager: User,
        rice: InventoryItem,
    ) -> None:
        batch = _run(
            organization=organization,
            kind=ImportKind.PURCHASE_REQUEST_DRAFT,
            raw=_csv(
                "warehouse_code,required_date,purpose,item_code,quantity",
                "MAIN,2026-09-01,تموين الأسبوع,RICE,50.000",
                "MAIN,2026-09-01,مناسبة خاصة,RICE,20.000",
            ),
            actor=manager,
            branch=branch,
        )
        assert batch.status == ImportBatchStatus.VALIDATED
        with audit_context(actor=manager):
            apply_batch(batch=batch)
        assert PurchaseRequest.objects.count() == 2

    def test_the_kind_requires_its_branch(self, organization: Organization, manager: User) -> None:
        with audit_context(actor=manager), pytest.raises(ValidationError) as refusal:
            create_batch(
                organization=organization,
                kind=ImportKind.PURCHASE_REQUEST_DRAFT,
                raw=_csv("warehouse_code,required_date,purpose,item_code,quantity"),
                filename="upload.csv",
            )
        assert refusal.value.code == "import_branch_required"


class TestBoundaryAndAccess:
    def test_no_import_kind_names_a_posted_document(self) -> None:
        """§16.8 by vocabulary: what has no kind cannot be uploaded."""
        from apps.inventory.imports import supported_kinds

        assert set(supported_kinds()) == {
            "ITEM_CATEGORY",
            "PACKAGE_UNIT",
            "BRANCH_ITEM_SETTING",
            "SUPPLIER",
            "SUPPLIER_ITEM",
            "PURCHASE_REQUEST_DRAFT",
        }
        forbidden = ("RECEIPT", "INVOICE", "RETURN", "CREDIT", "PAYMENT", "ORDER")
        for kind in supported_kinds():
            assert not any(word in kind for word in forbidden), kind

    def test_each_procurement_kind_demands_its_own_permission(self) -> None:
        from apps.inventory.import_views import permission_for_kind

        assert permission_for_kind(ImportKind.SUPPLIER) == "procurement.import_supplier"
        assert permission_for_kind(ImportKind.SUPPLIER_ITEM) == "procurement.import_supplier_item"
        assert (
            permission_for_kind(ImportKind.PURCHASE_REQUEST_DRAFT)
            == "procurement.create_purchase_request"
        )
        # The inventory kinds are untouched by the registration.
        assert permission_for_kind(ImportKind.ITEM_CATEGORY) == "inventory.import_master_data"

    def test_a_storekeeper_cannot_act_on_a_supplier_batch(
        self,
        organization: Organization,
        manager: User,
        keeper: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        The detail screen's commands check the kind's permission plus the
        batch's own organization — a storekeeper who can see import history
        still cannot reshape the supplier master.
        """
        batch = _run(
            organization=organization,
            kind=ImportKind.SUPPLIER,
            raw=_csv("code,name_ar", "GROC-01,مورد"),
            actor=manager,
        )
        response = client_for(keeper).post(
            reverse("inventory:import_detail", args=[batch.pk]), {"action": "apply"}
        )
        assert response.status_code == 403
        batch.refresh_from_db()
        assert batch.status == ImportBatchStatus.VALIDATED
        assert Supplier.objects.count() == 0
