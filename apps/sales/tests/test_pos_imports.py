from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.organizations.models import Role
from apps.organizations.services import grant_branch_access
from apps.sales.models import PosSalesImportBatch, PosSalesImportFile, PosSalesImportStatus
from apps.sales.permissions import (
    CONFIRM_POS_SALES_IMPORT,
    POST_POS_SALES_IMPORT,
    REVIEW_POS_SALES_IMPORT,
    VIEW_POS_SALES_IMPORT,
    permissions_for_role,
)
from apps.sales.pos_closing import (
    confirm_by_cashier,
    save_review_step,
    start_accountant_review,
)
from apps.sales.pos_imports import ParsedReport, normalize_name, reconcile
from apps.users.models import User

DATE = datetime.date(2026, 8, 27)


def _reports(*, category_total: str = "5709500") -> dict[str, ParsedReport]:
    total = Decimal("5709500")
    quantity = Decimal("779")
    app = Decimal("3195000")
    expenses = Decimal("3654500")
    data: dict[str, dict[str, Any]] = {
        "sales_items": {"total_sales": total, "total_quantity": quantity, "items": []},
        "sales_final": {
            "total_sales": total,
            "total_expenses": expenses,
            "net_sales": total,
            "net_cash": Decimal("2055000"),
        },
        "item_sales_by_type": {"total_sales": total, "total_quantity": quantity, "items": []},
        "sales_by_type": {
            "total_sales": total,
            "lines": [{"name": "ديليفري تطبيق", "amount": app}],
        },
        "sales_by_category": {"total_sales": Decimal(category_total), "lines": []},
        "expenses": {
            "total_expenses": expenses,
            "application_sales": app,
            "operational_expenses": Decimal("459500"),
            "lines": [],
        },
    }
    return {key: ParsedReport(key, DATE, value) for key, value in data.items()}


def test_reconciliation_separates_application_receipts_from_operational_expenses() -> None:
    headline, checks = reconcile(_reports())

    assert headline["total_sales"] == Decimal("5709500")
    assert headline["application_sales"] == Decimal("3195000")
    assert headline["reported_expenses"] == Decimal("3654500")
    assert headline["operational_expenses"] == Decimal("459500")
    assert headline["net_cash"] == Decimal("2055000")
    assert all(check["ok"] for check in checks)


def test_reconciliation_refuses_a_summary_that_disagrees_with_details() -> None:
    with pytest.raises(ValidationError, match="إجمالي المبيعات"):
        reconcile(_reports(category_total="5709000"))


def test_name_normalization_handles_pos_punctuation_and_spacing() -> None:
    assert normalize_name("  مندي  لحم . ") == "مندي لحم"


@pytest.mark.django_db
def test_import_pages_are_available_to_a_branch_manager(manager: User, client_for: Any) -> None:
    client = client_for(manager)

    assert client.get("/sales/pos-imports/").status_code == 200
    response = client.get("/sales/pos-imports/new/")
    assert response.status_code == 200
    assert "التقارير الستة" in response.content.decode()


def test_pos_closing_permissions_are_separated_between_cashier_and_accountant() -> None:
    cashier_permissions = permissions_for_role(Role.CASHIER)
    accountant_permissions = permissions_for_role(Role.ACCOUNTANT)

    assert CONFIRM_POS_SALES_IMPORT in cashier_permissions
    assert VIEW_POS_SALES_IMPORT in cashier_permissions
    assert REVIEW_POS_SALES_IMPORT not in cashier_permissions
    assert POST_POS_SALES_IMPORT not in cashier_permissions
    assert REVIEW_POS_SALES_IMPORT in accountant_permissions
    assert VIEW_POS_SALES_IMPORT in accountant_permissions
    assert POST_POS_SALES_IMPORT in accountant_permissions
    assert CONFIRM_POS_SALES_IMPORT not in accountant_permissions


def _workflow_batch(branch: Any, cashier: User) -> PosSalesImportBatch:
    batch = PosSalesImportBatch.objects.create(
        organization=branch.organization,
        branch=branch,
        business_date=DATE,
        status=PosSalesImportStatus.AWAITING_CASHIER,
        source_hash="f" * 64,
        total_sales=Decimal("100000"),
        application_sales=Decimal("20000"),
        reported_expenses=Decimal("5000"),
        operational_expenses=Decimal("5000"),
        net_cash=Decimal("75000"),
        total_quantity=Decimal("10"),
        report_data={"expenses": {"lines": []}, "item_sales_by_type": {"channel_totals": {}}},
        checks=[{"ok": True}],
        warnings=[],
        created_by=cashier,
    )
    for index, report_type in enumerate(
        [
            "sales_items",
            "sales_final",
            "item_sales_by_type",
            "sales_by_type",
            "sales_by_category",
            "expenses",
        ]
    ):
        PosSalesImportFile.objects.create(
            batch=batch,
            report_type=report_type,
            original_name=f"{report_type}.xlsx",
            file=f"sales/test/{report_type}.xlsx",
            checksum=str(index) * 64,
            size=10,
        )
    return batch


@pytest.mark.django_db
def test_cashier_confirmation_locks_batch_and_accountant_steps_are_sequential(
    branch: Any, cashier: User
) -> None:
    batch = _workflow_batch(branch, cashier)
    accountant = User.objects.create_user(username="pos-accountant", password="pw")
    grant_branch_access(user=accountant, branch=branch, role=Role.ACCOUNTANT)

    confirmed = confirm_by_cashier(batch=batch, actor=cashier)
    assert confirmed.status == PosSalesImportStatus.AWAITING_ACCOUNTANT
    assert confirmed.cashier_confirmed_by == cashier

    review = start_accountant_review(batch=confirmed, actor=accountant)
    assert review.status == PosSalesImportStatus.ACCOUNTANT_REVIEW
    with pytest.raises(ValidationError, match="بالترتيب"):
        save_review_step(batch=review, actor=accountant, step=2, evidence={"approved": True})

    review = save_review_step(batch=review, actor=accountant, step=1, evidence={"approved": True})
    assert review.review_step == 1


@pytest.mark.django_db
def test_cashier_cannot_act_as_the_accountant_reviewer(branch: Any, cashier: User) -> None:
    batch = confirm_by_cashier(batch=_workflow_batch(branch, cashier), actor=cashier)

    with pytest.raises(ValidationError, match="مؤكد المبيعات"):
        start_accountant_review(batch=batch, actor=cashier)


@pytest.mark.django_db
def test_superuser_override_can_review_own_cashier_confirmation(branch: Any) -> None:
    administrator = User.objects.create_superuser(username="pos-owner", password="pw")
    batch = confirm_by_cashier(batch=_workflow_batch(branch, administrator), actor=administrator)

    review = start_accountant_review(batch=batch, actor=administrator)

    assert review.status == PosSalesImportStatus.ACCOUNTANT_REVIEW
    assert review.accountant_started_by == administrator
