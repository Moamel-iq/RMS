"""Effective-dated supplier credit terms and invoice approval snapshots."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.accounting.models import Account, CostCenter
from apps.accounting.services import configure_accounting, open_fiscal_year
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import grant_organization_access
from apps.procurement.credit_terms import (
    activate_credit_term,
    create_credit_term_draft,
    delete_credit_term_draft,
    resolve_credit_term,
    update_credit_term_draft,
)
from apps.procurement.invoices import (
    add_account_line,
    approve_supplier_invoice,
    create_supplier_invoice,
)
from apps.procurement.models import Supplier, SupplierCreditTerm, SupplierCreditTermStatus
from apps.procurement.services import create_supplier
from apps.users.models import User

pytestmark = pytest.mark.django_db

JAN_1 = datetime.date(2026, 1, 1)
MAR_1 = datetime.date(2026, 3, 1)
MAR_10 = datetime.date(2026, 3, 10)
PASSWORD = "pw-not-real-1234"


@pytest.fixture
def purchasing(organization: Organization) -> User:
    user = User.objects.create_user(username="credit-term-maker", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.PURCHASING)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def second_controller(organization: Organization) -> User:
    user = User.objects.create_user(username="credit-term-controller-2", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def supplier(organization: Organization) -> Supplier:
    return create_supplier(
        organization=organization,
        code="TERM-SUPPLIER",
        name="مورد الشروط",
        payment_terms_days=0,
    )


@pytest.fixture
def accounting(organization: Organization) -> None:
    configure_accounting(organization=organization, fiscal_year_start_month=1)
    open_fiscal_year(organization=organization, year=2026)
    call_command("seed_chart_of_accounts", organization=organization.code, verbosity=0)


def _replacement(
    *, supplier: Supplier, actor: User, days: int, effective_from: datetime.date
) -> SupplierCreditTerm:
    current = SupplierCreditTerm.objects.get(
        supplier=supplier, status=SupplierCreditTermStatus.ACTIVE
    )
    return create_credit_term_draft(
        supplier=supplier,
        name=f"{days} يوم",
        net_days=days,
        effective_from=effective_from,
        created_by=actor,
        supersedes=current,
        notes="تغيير تجريبي",
    )


class TestCreditTermLifecycle:
    def test_supplier_creation_bootstraps_one_visible_active_term(self, supplier: Supplier) -> None:
        term = SupplierCreditTerm.objects.get(supplier=supplier)

        assert term.status == SupplierCreditTermStatus.ACTIVE
        assert term.version == 1
        assert term.net_days == 0
        assert term.effective_from == datetime.date(1900, 1, 1)
        assert term.public_id is not None

    def test_only_one_draft_may_exist_per_supplier(
        self, supplier: Supplier, purchasing: User
    ) -> None:
        _replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1)

        with pytest.raises(ValidationError) as refusal:
            _replacement(supplier=supplier, actor=purchasing, days=30, effective_from=MAR_1)

        assert refusal.value.code == "credit_term_draft_exists"

    def test_creator_cannot_activate_their_own_term(
        self, supplier: Supplier, purchasing: User
    ) -> None:
        draft = _replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1)

        with pytest.raises(ValidationError) as refusal:
            activate_credit_term(term=draft, actor=purchasing)

        assert refusal.value.code == "credit_term_maker_checker"
        draft.refresh_from_db()
        assert draft.status == SupplierCreditTermStatus.DRAFT

    def test_activation_supersedes_and_closes_the_replaced_version(
        self, supplier: Supplier, purchasing: User, accounting_manager: User
    ) -> None:
        original = SupplierCreditTerm.objects.get(supplier=supplier)
        draft = _replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1)

        active = activate_credit_term(term=draft, actor=accounting_manager)

        original.refresh_from_db()
        supplier.refresh_from_db()
        assert original.status == SupplierCreditTermStatus.SUPERSEDED
        assert original.effective_to == MAR_1 - datetime.timedelta(days=1)
        assert active.status == SupplierCreditTermStatus.ACTIVE
        assert active.approved_by == accounting_manager
        assert supplier.payment_terms_days == 14

    def test_stale_instance_cannot_be_activated_twice(
        self,
        supplier: Supplier,
        purchasing: User,
        accounting_manager: User,
        second_controller: User,
    ) -> None:
        draft = _replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1)
        stale = SupplierCreditTerm.objects.get(pk=draft.pk)
        activate_credit_term(term=draft, actor=accounting_manager)

        with pytest.raises(ValidationError) as refusal:
            activate_credit_term(term=stale, actor=second_controller)

        assert refusal.value.code == "credit_term_not_draft"

    def test_only_a_draft_can_be_edited_or_deleted(
        self, supplier: Supplier, purchasing: User, accounting_manager: User
    ) -> None:
        draft = _replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1)
        updated = update_credit_term_draft(
            term=draft,
            name="أربعة عشر يوماً",
            net_days=14,
            effective_from=MAR_1,
            effective_to=None,
            notes="تصحيح المسودة",
        )
        active = activate_credit_term(term=updated, actor=accounting_manager)

        with pytest.raises(ValidationError) as edit_refusal:
            update_credit_term_draft(
                term=active,
                name="ممنوع",
                net_days=30,
                effective_from=MAR_1,
                effective_to=None,
                notes="",
            )
        with pytest.raises(ValidationError) as delete_refusal:
            delete_credit_term_draft(term=active)

        assert edit_refusal.value.code == "credit_term_not_draft"
        assert delete_refusal.value.code == "credit_term_not_draft"

    def test_database_rejects_mutating_an_activated_term(
        self, supplier: Supplier, purchasing: User, accounting_manager: User
    ) -> None:
        active = activate_credit_term(
            term=_replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1),
            actor=accounting_manager,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierCreditTerm.objects.filter(pk=active.pk).update(net_days=99)

    def test_inclusive_active_ranges_cannot_overlap(
        self, supplier: Supplier, purchasing: User, accounting_manager: User
    ) -> None:
        first = activate_credit_term(
            term=_replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1),
            actor=accounting_manager,
        )
        # Insert through the ORM to prove the database, not only the service,
        # owns the inclusive-range invariant.
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplierCreditTerm.objects.create(
                organization=first.organization,
                supplier=first.supplier,
                version=first.version + 1,
                status=SupplierCreditTermStatus.ACTIVE,
                name="متداخل",
                net_days=30,
                effective_from=first.effective_from,
                created_by=purchasing,
                approved_by=accounting_manager,
                approved_at=first.approved_at,
            )

    def test_superseded_version_remains_authoritative_for_its_closed_period(
        self, supplier: Supplier, purchasing: User, accounting_manager: User
    ) -> None:
        original = SupplierCreditTerm.objects.get(supplier=supplier)
        replacement = activate_credit_term(
            term=_replacement(
                supplier=supplier,
                actor=purchasing,
                days=14,
                effective_from=MAR_1,
            ),
            actor=accounting_manager,
        )

        assert resolve_credit_term(supplier=supplier, on=JAN_1) == original
        assert resolve_credit_term(supplier=supplier, on=MAR_10) == replacement


class TestInvoiceCreditTermSnapshot:
    def test_approval_freezes_the_effective_term_and_due_date(
        self,
        supplier: Supplier,
        branch: Branch,
        purchasing: User,
        accounting_manager: User,
        second_controller: User,
        accounting: None,
    ) -> None:
        term_14 = activate_credit_term(
            term=_replacement(supplier=supplier, actor=purchasing, days=14, effective_from=MAR_1),
            actor=accounting_manager,
        )
        invoice = create_supplier_invoice(
            supplier=supplier,
            branch=branch,
            created_by=purchasing,
            supplier_invoice_number="TERM-INV-1",
            invoice_date=MAR_10,
            business_date=MAR_10,
        )
        account = Account.objects.get(organization=term_14.organization, code="5-01-02-003")
        cost_center = CostCenter.objects.get(organization=term_14.organization, code="DELIVERY")
        add_account_line(
            invoice=invoice,
            account=account,
            cost_center=cost_center,
            description="اختبار شروط السداد",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1000.000000"),
        )

        approved = approve_supplier_invoice(invoice=invoice, actor=accounting_manager)

        assert approved.credit_term_id == term_14.pk
        assert approved.credit_term_public_id == term_14.public_id
        assert approved.credit_term_version == 2
        assert approved.credit_term_name == "14 يوم"
        assert approved.credit_term_net_days == 14
        assert approved.payment_terms_days == 14
        assert approved.due_date == MAR_10 + datetime.timedelta(days=14)

        term_30 = create_credit_term_draft(
            supplier=supplier,
            name="30 يوم",
            net_days=30,
            effective_from=MAR_10,
            created_by=second_controller,
            supersedes=term_14,
        )
        activate_credit_term(term=term_30, actor=purchasing)
        approved.refresh_from_db()

        assert approved.credit_term_public_id == term_14.public_id
        assert approved.credit_term_net_days == 14
        assert approved.due_date == MAR_10 + datetime.timedelta(days=14)


class TestCreditTermSurface:
    def test_list_full_page_and_htmx_fragment_have_the_same_term(
        self, supplier: Supplier, purchasing: User, client: Client
    ) -> None:
        client.force_login(purchasing)
        url = reverse("procurement:credit_term_list")

        full = client.get(url, {"days": "0"})
        fragment = client.get(url, {"days": "0"}, HTTP_HX_REQUEST="true")

        assert full.status_code == 200
        assert fragment.status_code == 200
        assert supplier.code in full.content.decode()
        fragment_body = fragment.content.decode()
        assert supplier.code in fragment_body
        assert "<html" not in fragment_body.lower()

    def test_htmx_create_and_activation_use_redirect_headers(
        self,
        supplier: Supplier,
        purchasing: User,
        accounting_manager: User,
        client: Client,
    ) -> None:
        client.force_login(purchasing)
        created = client.post(
            reverse("procurement:credit_term_create"),
            {
                "supplier": supplier.pk,
                "name": "14 يوم",
                "net_days": "14",
                "effective_from": "2026-03-01",
                "effective_to": "",
                "notes": "مسودة من الواجهة",
            },
            HTTP_HX_REQUEST="true",
        )
        assert created.status_code == 200
        assert created.headers["HX-Redirect"] == reverse("procurement:credit_term_list")
        draft = SupplierCreditTerm.objects.get(
            supplier=supplier, status=SupplierCreditTermStatus.DRAFT
        )

        client.force_login(accounting_manager)
        activated = client.post(
            reverse("procurement:credit_term_activate", args=[draft.pk]),
            HTTP_HX_REQUEST="true",
        )
        assert activated.status_code == 200
        assert activated.headers["HX-Redirect"] == reverse(
            "procurement:credit_term_detail", args=[draft.pk]
        )
        draft.refresh_from_db()
        assert draft.status == SupplierCreditTermStatus.ACTIVE

    def test_in_scope_but_unauthorized_activation_is_403(
        self, supplier: Supplier, purchasing: User, client: Client
    ) -> None:
        draft = _replacement(
            supplier=supplier,
            actor=purchasing,
            days=14,
            effective_from=MAR_1,
        )
        client.force_login(purchasing)

        response = client.post(reverse("procurement:credit_term_activate", args=[draft.pk]))

        assert response.status_code == 403
        draft.refresh_from_db()
        assert draft.status == SupplierCreditTermStatus.DRAFT

    def test_foreign_term_is_404_in_ui_and_api(
        self,
        supplier: Supplier,
        other_organization: Organization,
        client: Client,
    ) -> None:
        outsider = User.objects.create_user(username="term-outsider", password=PASSWORD)
        grant_organization_access(
            user=outsider,
            organization=other_organization,
            role=Role.ACCOUNTING_MANAGER,
        )
        client.force_login(User.objects.get(pk=outsider.pk))
        term = SupplierCreditTerm.objects.get(supplier=supplier)

        assert (
            client.get(reverse("procurement:credit_term_detail", args=[term.pk])).status_code == 404
        )
        assert (
            client.get(f"/api/v1/procurement/supplier-credit-terms/{term.pk}/").status_code == 404
        )

    def test_api_drives_draft_edit_activation_and_exposes_versions(
        self,
        supplier: Supplier,
        purchasing: User,
        accounting_manager: User,
        client: Client,
    ) -> None:
        client.force_login(purchasing)
        created = client.post(
            "/api/v1/procurement/supplier-credit-terms/",
            data={
                "supplier_id": supplier.pk,
                "name": "14 يوم",
                "net_days": 14,
                "effective_from": "2026-03-01",
            },
            content_type="application/json",
        )
        assert created.status_code == 201
        term_id = created.json()["id"]
        updated = client.put(
            f"/api/v1/procurement/supplier-credit-terms/{term_id}/",
            data={
                "name": "أربعة عشر يوماً",
                "net_days": 14,
                "effective_from": "2026-03-01",
                "notes": "تصحيح API",
            },
            content_type="application/json",
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2

        client.force_login(accounting_manager)
        activated = client.post(f"/api/v1/procurement/supplier-credit-terms/{term_id}/activate/")
        assert activated.status_code == 200
        assert activated.json()["status"] == SupplierCreditTermStatus.ACTIVE
        assert activated.json()["net_days"] == 14
