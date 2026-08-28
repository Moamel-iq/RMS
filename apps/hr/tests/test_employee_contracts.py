"""Employee master and effective-dated contract acceptance tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from datetime import time
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditAction, AuditEvent
from apps.hr.models import (
    ContractStatus,
    ContractType,
    Employee,
    EmployeeContract,
    EmployeePaymentMethod,
    EmployeeStatus,
    PayrollPolicy,
    WageBasis,
)
from apps.hr.permissions import (
    ALL_PERMISSIONS,
    APPROVE_CONTRACT,
    MANAGE_CONTRACT,
    VIEW_EMPLOYEE_PERSONAL,
    VIEW_EMPLOYEE_SALARY,
    PermissionScope,
    permissions_for_role,
    scope_of,
)
from apps.hr.services import (
    add_employee_document,
    approve_contract,
    create_contract,
    create_employee,
    default_policy_values,
    terminate_employee,
)
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_organization_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM-HR", name="خان مندي")


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="OTHER-HR", name="منافس")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK-HR",
        name="البنوك",
        business_day_start_time=time(9),
    )


def _actor(username: str, organization: Organization, role: Role) -> User:
    user = User.objects.create_user(username=username, password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=role)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def maker(organization: Organization) -> User:
    return _actor("hr-maker", organization, Role.MANAGER)


@pytest.fixture
def checker(organization: Organization) -> User:
    return _actor("hr-checker", organization, Role.OWNER)


@pytest.fixture
def viewer(organization: Organization) -> User:
    return _actor("hr-viewer", organization, Role.VIEWER)


@pytest.fixture
def policy(organization: Organization, maker: User) -> PayrollPolicy:
    return default_policy_values(organization=organization, actor=maker)


@pytest.fixture
def employee(organization: Organization, branch: Branch, maker: User) -> Employee:
    return create_employee(
        organization=organization,
        code="EMP-001",
        name="أحمد علي",
        phone="07700000001",
        email="ahmed@example.test",
        identity_number="ID-HR-SECRET-1",
        date_of_birth=datetime.date(1990, 5, 2),
        gender="ذكر",
        marital_status="متزوج",
        address="بغداد",
        emergency_contact="07700000002",
        branch=branch,
        department="العمليات",
        job_title="مشرف صالة",
        workplace="فرع البنوك",
        hire_date=datetime.date(2026, 1, 1),
        payment_method=EmployeePaymentMethod.BANK,
        payment_reference="BANK-HR-SECRET-1",
        notes="",
        actor=maker,
    )


def _contract(
    *,
    employee: Employee,
    policy: PayrollPolicy,
    actor: User,
    start: datetime.date,
    end: datetime.date | None = None,
    salary: str = "1000000.000",
) -> EmployeeContract:
    return create_contract(
        employee=employee,
        actor=actor,
        fixed_allowances=[{"name": "بدل نقل", "amount": "50000.000"}],
        contract_type=ContractType.PERMANENT,
        start_date=start,
        end_date=end,
        branch=employee.branch,
        job_title=employee.job_title,
        department=employee.department,
        wage_basis=WageBasis.MONTHLY,
        basic_salary=Decimal(salary),
        scheduled_work_days=Decimal("26.000"),
        scheduled_hours=Decimal("208.000"),
        probation_days=90,
        default_shift_code="DAY",
        payment_method=EmployeePaymentMethod.BANK,
        payroll_policy=policy,
        notes="",
    )


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    def _login(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return _login


def test_employee_code_is_permanent_and_the_database_refuses_hard_delete(
    employee: Employee,
    organization: Organization,
    branch: Branch,
    maker: User,
) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        Employee.objects.filter(pk=employee.pk).delete()

    with pytest.raises((ValidationError, IntegrityError)):
        create_employee(
            organization=organization,
            code="emp-001",
            name="موظف آخر",
            phone="",
            email="",
            identity_number="",
            date_of_birth=None,
            gender="",
            marital_status="",
            address="",
            emergency_contact="",
            branch=branch,
            department="",
            job_title="عامل",
            workplace="",
            hire_date=datetime.date(2026, 2, 1),
            payment_method=EmployeePaymentMethod.CASH,
            payment_reference="",
            notes="",
            actor=maker,
        )


def test_contract_requires_a_different_approver_and_freezes_salary_in_the_database(
    employee: Employee,
    policy: PayrollPolicy,
    maker: User,
    checker: User,
) -> None:
    contract = _contract(
        employee=employee,
        policy=policy,
        actor=maker,
        start=datetime.date(2026, 1, 1),
    )
    with pytest.raises(ValidationError, match="creator cannot approve"):
        approve_contract(contract=contract, actor=maker)

    approved = approve_contract(contract=contract, actor=checker)
    assert approved.status == ContractStatus.APPROVED
    assert approved.approved_by == checker
    assert approved.approved_at is not None

    with pytest.raises(DatabaseError), transaction.atomic():
        EmployeeContract.objects.filter(pk=approved.pk).update(basic_salary=Decimal("9999999.000"))
    approved.refresh_from_db()
    assert approved.basic_salary == Decimal("1000000.000")


def test_a_later_approved_version_supersedes_the_previous_salary_snapshot(
    employee: Employee,
    policy: PayrollPolicy,
    maker: User,
    checker: User,
) -> None:
    original = approve_contract(
        contract=_contract(
            employee=employee,
            policy=policy,
            actor=maker,
            start=datetime.date(2026, 1, 1),
        ),
        actor=checker,
    )
    replacement = approve_contract(
        contract=_contract(
            employee=employee,
            policy=policy,
            actor=maker,
            start=datetime.date(2026, 7, 1),
            salary="1200000.000",
        ),
        actor=checker,
    )

    original.refresh_from_db()
    assert original.status == ContractStatus.SUPERSEDED
    assert original.end_date == datetime.date(2026, 6, 30)
    assert replacement.status == ContractStatus.APPROVED
    assert replacement.version == 2


def test_termination_closes_current_and_cancels_future_or_draft_contracts(
    employee: Employee,
    policy: PayrollPolicy,
    maker: User,
    checker: User,
) -> None:
    current = approve_contract(
        contract=_contract(
            employee=employee,
            policy=policy,
            actor=maker,
            start=datetime.date(2026, 1, 1),
            end=datetime.date(2026, 6, 30),
        ),
        actor=checker,
    )
    future = approve_contract(
        contract=_contract(
            employee=employee,
            policy=policy,
            actor=maker,
            start=datetime.date(2026, 7, 1),
        ),
        actor=checker,
    )
    draft = _contract(
        employee=employee,
        policy=policy,
        actor=maker,
        start=datetime.date(2027, 1, 1),
    )

    terminated = terminate_employee(
        employee=employee,
        termination_date=datetime.date(2026, 5, 31),
        reason="انتهاء الحاجة التشغيلية",
    )

    current.refresh_from_db()
    future.refresh_from_db()
    draft.refresh_from_db()
    assert terminated.status == EmployeeStatus.TERMINATED
    assert current.status == ContractStatus.CLOSED
    assert current.end_date == datetime.date(2026, 5, 31)
    assert future.status == ContractStatus.CANCELLED
    assert future.end_date is None
    assert draft.status == ContractStatus.CANCELLED
    assert employee.history.count() >= 2


def test_viewer_sees_the_employee_but_not_personal_salary_or_contract_details(
    employee: Employee,
    policy: PayrollPolicy,
    maker: User,
    checker: User,
    viewer: User,
    client_for: Callable[[User], Client],
) -> None:
    contract = approve_contract(
        contract=_contract(
            employee=employee,
            policy=policy,
            actor=maker,
            start=datetime.date(2026, 1, 1),
        ),
        actor=checker,
    )
    response = client_for(viewer).get(reverse("hr:employee_detail", args=[employee.pk]))

    assert response.status_code == 200
    content = response.content.decode()
    assert employee.name in content
    assert "ID-HR-SECRET-1" not in content
    assert "BANK-HR-SECRET-1" not in content
    assert "1000000" not in content
    assert reverse("hr:contract_detail", args=[contract.pk]) not in content
    assert (
        client_for(viewer).get(reverse("hr:contract_detail", args=[contract.pk])).status_code == 403
    )


def test_employee_documents_use_a_personal_data_gate_and_audited_download(
    employee: Employee,
    maker: User,
    viewer: User,
    client_for: Callable[[User], Client],
) -> None:
    document = add_employee_document(
        employee=employee,
        actor=maker,
        document_type="هوية",
        title="هوية الموظف",
        reference="ID-HR-SECRET-1",
        file=SimpleUploadedFile("identity.pdf", b"not-a-real-pdf"),
        issue_date=None,
        expiry_date=None,
        notes="",
    )
    download_url = reverse("hr:employee_document_download", args=[employee.pk, document.pk])

    viewer_page = client_for(viewer).get(reverse("hr:employee_detail", args=[employee.pk]))
    assert "هوية الموظف" not in viewer_page.content.decode()
    assert client_for(viewer).get(download_url).status_code == 403

    response = client_for(maker).get(download_url)
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    # The attachment is a FileResponse, so the body arrives as a stream;
    # getvalue() joins the chunks the same way iterating the response would.
    assert b"not-a-real-pdf" == response.getvalue()
    assert client_for(maker).get(document.file.url).status_code == 404
    assert AuditEvent.objects.filter(
        action=AuditAction.DOCUMENT_DOWNLOADED,
        target_type="hr.EmployeeDocument",
        target_id=str(document.pk),
        organization=employee.organization,
    ).exists()


def test_an_out_of_scope_employee_is_indistinguishable_from_a_missing_row(
    employee: Employee,
    other_organization: Organization,
    client_for: Callable[[User], Client],
) -> None:
    outsider = _actor("hr-outsider", other_organization, Role.MANAGER)
    response = client_for(outsider).get(reverse("hr:employee_detail", args=[employee.pk]))
    assert response.status_code == 404


def test_arabic_employee_and_contract_workspaces_keep_htmx_progressive_enhancement(
    employee: Employee,
    maker: User,
    client_for: Callable[[User], Client],
) -> None:
    client = client_for(maker)
    employee_list = client.get(reverse("hr:employee_list"))
    employee_form = client.get(reverse("hr:employee_create"), HTTP_HX_REQUEST="true")
    contract_list = client.get(reverse("hr:contract_list"))

    assert employee_list.status_code == 200
    assert employee.name in employee_list.content.decode()
    assert 'hx-get="/hr/employees/"' in employee_list.content.decode()
    assert employee_form.status_code == 200
    assert "<html" not in employee_form.content.decode().lower()
    assert f'hx-post="{reverse("hr:employee_create")}"' in employee_form.content.decode()
    assert contract_list.status_code == 200
    assert "العقود والأجور" in contract_list.content.decode()


def test_every_hr_permission_is_scoped_and_the_role_matrix_separates_sensitive_data() -> None:
    assert set(ALL_PERMISSIONS) == {
        "hr.view_employee_workspace",
        "hr.manage_employee",
        "hr.terminate_employee",
        "hr.view_employee_personal",
        "hr.view_employee_salary",
        "hr.view_contract_workspace",
        "hr.manage_contract",
        "hr.approve_contract",
        "hr.view_shift_workspace",
        "hr.manage_shift",
        "hr.assign_shift",
        "hr.view_attendance_workspace",
        "hr.record_attendance",
        "hr.correct_attendance",
        "hr.approve_attendance",
        "hr.view_leave_workspace",
        "hr.request_leave",
        "hr.approve_leave",
        "hr.classify_absence",
        "hr.view_overtime_workspace",
        "hr.manage_overtime",
        "hr.approve_overtime",
        "hr.view_deduction_workspace",
        "hr.manage_deduction",
        "hr.approve_deduction",
        "hr.view_advance_workspace",
        "hr.manage_advance",
        "hr.approve_advance",
        "hr.disburse_advance",
        "hr.view_payroll_workspace",
        "hr.calculate_payroll",
        "hr.review_payroll",
        "hr.approve_payroll",
        "hr.post_payroll",
        "hr.pay_payroll",
        "hr.view_payroll_amounts",
        "hr.view_employee_statement",
    }
    assert all(
        scope_of(permission) is PermissionScope.ORGANIZATION_AUTHORITY
        for permission in ALL_PERMISSIONS
    )
    assert VIEW_EMPLOYEE_PERSONAL in permissions_for_role(Role.MANAGER)
    assert VIEW_EMPLOYEE_PERSONAL not in permissions_for_role(Role.ACCOUNTANT)
    assert VIEW_EMPLOYEE_SALARY in permissions_for_role(Role.ACCOUNTANT)
    assert MANAGE_CONTRACT not in permissions_for_role(Role.ACCOUNTANT)
    assert APPROVE_CONTRACT in permissions_for_role(Role.ACCOUNTING_MANAGER)


def test_the_employee_and_contract_pages_are_drawn_inside_the_shell(
    employee: Employee,
    policy: PayrollPolicy,
    maker: User,
    checker: User,
    client_for: Callable[[User], Client],
) -> None:
    """
    Both pages once overrode `block content`, which is the whole document —
    so they rendered without navigation, top bar, skip link, `<main>` or the
    confirmation dialog, and a keyboard reader arriving at an employee file
    had no way back out. They fill `block page`, like every other screen.
    """
    contract = approve_contract(
        contract=_contract(
            employee=employee,
            policy=policy,
            actor=maker,
            start=datetime.date(2026, 1, 1),
        ),
        actor=checker,
    )
    client = client_for(checker)
    pages = {
        "employee": reverse("hr:employee_detail", args=[employee.pk]),
        "contract": reverse("hr:contract_detail", args=[contract.pk]),
    }
    for name, url in pages.items():
        body = client.get(url).content.decode()
        assert 'class="ui-app-header"' in body, name
        assert 'id="main-content"' in body, name
        assert "ui-skip-link" in body, name
        assert "data-shell-nav" in body, name
        assert "data-confirm-dialog" in body, name
        # And the page still carries its own title, not the bare system name.
        assert "<title>" in body and "· نظام خان مندي" in body, name
