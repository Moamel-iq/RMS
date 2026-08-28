"""Seed reversible Human Resources master-data examples without payroll posting."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.hr.models import (
    ContractType,
    Employee,
    EmployeeContract,
    EmployeePaymentMethod,
    EmployeeStatus,
    WageBasis,
)
from apps.hr.services import (
    approve_contract,
    archive_employee,
    create_contract,
    create_employee,
    default_policy_values,
    terminate_employee,
)
from apps.organizations.models import Branch, Organization
from apps.users.models import User


class Command(BaseCommand):
    help = "Seed idempotent HR employees and contract states; never posts payroll or journals."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--organization", default="DEMO-KHAN-MANDI")
        parser.add_argument("--actor", default="moamel")
        parser.add_argument("--approver", default="demo-sales-accounting")

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        try:
            organization = Organization.objects.get(code=options["organization"])
            actor = User.objects.get(username=options["actor"], is_active=True)
            approver = User.objects.get(username=options["approver"], is_active=True)
        except (Organization.DoesNotExist, User.DoesNotExist) as error:
            raise CommandError(str(error)) from error
        branch = (
            Branch.objects.filter(organization=organization, is_active=True).order_by("id").first()
        )
        if branch is None:
            raise CommandError(f"{organization.code} has no active branch")
        if actor.pk == approver.pk:
            raise CommandError("Demo actor and approver must be different users")

        policy = default_policy_values(organization=organization, actor=actor)
        supervisor = self._employee(
            organization=organization,
            branch=branch,
            actor=actor,
            code="HR-DEMO-001",
            name="أحمد كريم",
            job_title="مشرف صالة",
            department="العمليات",
            hire_date=datetime.date(2025, 1, 1),
        )
        cashier = self._employee(
            organization=organization,
            branch=branch,
            actor=actor,
            code="HR-DEMO-002",
            name="سارة علي",
            job_title="أمينة صندوق",
            department="المبيعات",
            hire_date=datetime.date(2025, 6, 1),
        )
        former = self._employee(
            organization=organization,
            branch=branch,
            actor=actor,
            code="HR-DEMO-003",
            name="حسن جاسم",
            job_title="عامل تحضير",
            department="المطبخ",
            hire_date=datetime.date(2025, 2, 1),
        )
        archived = self._employee(
            organization=organization,
            branch=branch,
            actor=actor,
            code="HR-DEMO-004",
            name="ملف تدريبي مؤرشف",
            job_title="متدرب",
            department="التدريب",
            hire_date=datetime.date(2025, 8, 1),
        )

        self._approved_contract(
            employee=supervisor,
            policy=policy,
            actor=actor,
            approver=approver,
            start=datetime.date(2025, 1, 1),
            salary="1500000.000",
        )
        if not cashier.contracts.exists():
            self._create_contract(
                employee=cashier,
                policy=policy,
                actor=actor,
                start=datetime.date(2026, 1, 1),
                salary="1000000.000",
            )
        self._approved_contract(
            employee=former,
            policy=policy,
            actor=actor,
            approver=approver,
            start=datetime.date(2025, 2, 1),
            salary="850000.000",
        )
        if former.status != EmployeeStatus.TERMINATED:
            terminate_employee(
                employee=former,
                termination_date=datetime.date(2026, 3, 31),
                reason="بيانات عرض: انتهاء عقد تشغيلي",
            )
        if archived.status != EmployeeStatus.ARCHIVED:
            archive_employee(employee=archived, reason="بيانات عرض: ملف تدريبي مؤرشف")

        self.stdout.write(
            self.style.SUCCESS(
                f"HR demo ready for {organization.code}: 4 employees, "
                f"{EmployeeContract.objects.filter(organization=organization).count()} contracts; "
                "no payroll or journal was posted."
            )
        )

    def _employee(
        self,
        *,
        organization: Organization,
        branch: Branch,
        actor: User,
        code: str,
        name: str,
        job_title: str,
        department: str,
        hire_date: datetime.date,
    ) -> Employee:
        existing = Employee.objects.filter(organization=organization, code=code).first()
        if existing is not None:
            return existing
        return create_employee(
            organization=organization,
            code=code,
            name=name,
            phone="07700000000",
            email=f"{code.lower()}@example.test",
            identity_number=f"DEMO-{code}",
            date_of_birth=None,
            gender="",
            marital_status="",
            address="بغداد",
            emergency_contact="",
            branch=branch,
            department=department,
            job_title=job_title,
            workplace=branch.name,
            hire_date=hire_date,
            payment_method=EmployeePaymentMethod.BANK,
            payment_reference=f"DEMO-BANK-{code}",
            notes="بيانات عرض قابلة للمراجعة",
            actor=actor,
        )

    def _create_contract(
        self,
        *,
        employee: Employee,
        policy: Any,
        actor: User,
        start: datetime.date,
        salary: str,
    ) -> EmployeeContract:
        return create_contract(
            employee=employee,
            actor=actor,
            fixed_allowances=[{"name": "بدل نقل", "amount": "50000.000"}],
            contract_type=ContractType.PERMANENT,
            start_date=start,
            end_date=None,
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
            notes="بيانات عرض لعقد موظف",
        )

    def _approved_contract(
        self,
        *,
        employee: Employee,
        policy: Any,
        actor: User,
        approver: User,
        start: datetime.date,
        salary: str,
    ) -> EmployeeContract:
        existing = employee.contracts.order_by("version").first()
        if existing is not None:
            return existing
        contract = self._create_contract(
            employee=employee,
            policy=policy,
            actor=actor,
            start=start,
            salary=salary,
        )
        return approve_contract(contract=contract, actor=approver)
