r"""
Load employees, and optionally their contracts, from a transcribed payroll file.

    .venv\Scripts\python.exe manage.py import_employees ^
        --organization 01 --branch 011 --actor <username> ^
        --file "<a path outside this repository>\employees.json"

Add `--with-contracts --work-days 30 --hours-per-day 10` to create a monthly
contract per employee carrying the basic salary.

**This data must never enter the repository.** Names and salaries of real
people are the one category `CLAUDE.md` names twice, and this repository has a
remote. The loader therefore takes `--file` with no default that could point
inside the tree, and the payroll figures live wherever the owner keeps them.

Contracts are separate and opt-in because a contract needs two facts a payroll
sheet does not carry: how many days a month the employee is scheduled for, and
how many hours a day. The July sheet shows every employee credited for every
calendar day between joining and month end — no weekly rest anywhere — so the
schedule cannot be inferred from it either. Asking for the numbers is the only
honest way to put them in, so they are required arguments rather than defaults
somebody would later mistake for a fact.

Contracts land as DRAFT. A contract is an agreement between two parties and an
importer is neither of them.
"""

from __future__ import annotations

import csv
import datetime
import json
import pathlib
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.core.console import SeedCommand
from apps.hr.models import ContractType, Employee, EmployeePaymentMethod, WageBasis
from apps.hr.services import create_contract, create_employee
from apps.organizations.models import Branch, Organization
from apps.users.models import User


def _load(path: pathlib.Path) -> dict[str, Any]:
    """Read the payroll from CSV or JSON. CSV is what a manager can maintain."""
    if path.suffix.lower() == ".json":
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    employees: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            name = (row.get("اسم الموظف") or "").strip()
            if not name:
                continue
            employees.append(
                {
                    "seq": int((row.get("ت") or index) or index),
                    "name": name,
                    "department": (row.get("القسم") or "").strip(),
                    "job_title": (row.get("المسمى الوظيفي") or "").strip(),
                    "basic_salary": (row.get("الراتب الأساسي") or "0").strip(),
                    "hire_date": (row.get("تاريخ المباشرة") or "").strip(),
                    "phone": (row.get("الهاتف") or "").strip(),
                }
            )
    return {"employees": employees}


class Command(SeedCommand):
    help = "Import employees from a payroll file kept outside this repository."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--organization", required=True)
        parser.add_argument("--branch", required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--with-contracts", action="store_true")
        parser.add_argument("--work-days", type=str, help="Scheduled work days per month.")
        parser.add_argument("--hours-per-day", type=str, help="Scheduled hours per work day.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        organization = Organization.objects.filter(code=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organization {options['organization']}.")
        branch = Branch.objects.filter(organization=organization, code=options["branch"]).first()
        if branch is None:
            raise CommandError(f"No branch {options['branch']}.")
        actor = User.objects.filter(username=options["actor"]).first()
        if actor is None:
            raise CommandError(f"No user {options['actor']}.")

        with_contracts = options["with_contracts"]
        if with_contracts and not (options["work_days"] and options["hours_per_day"]):
            raise CommandError(
                "--with-contracts needs --work-days and --hours-per-day. "
                "A payroll sheet does not state a schedule and this will not invent one."
            )

        path = pathlib.Path(options["file"])
        if not path.is_file():
            raise CommandError(f"{path} is missing.")
        payload = _load(path)

        made = reused = contracts = 0
        skipped: list[tuple[str, str]] = []

        with transaction.atomic():
            for row in payload["employees"]:
                code = f"EMP-{int(row['seq']):03d}"
                employee = Employee.objects.filter(organization=organization, code=code).first()
                if employee is None:
                    try:
                        employee = create_employee(
                            organization=organization,
                            code=code,
                            name=row["name"],
                            phone="",
                            email="",
                            identity_number="",
                            date_of_birth=None,
                            gender="",
                            marital_status="",
                            address="",
                            emergency_contact="",
                            branch=branch,
                            department=row["department"],
                            job_title=row["job_title"],
                            workplace=branch.name,
                            hire_date=datetime.date.fromisoformat(row["hire_date"]),
                            payment_method=EmployeePaymentMethod.CASH,
                            payment_reference="",
                            notes="مستورد من كشف الرواتب",
                            actor=actor,
                        )
                        made += 1
                    except ValidationError as refused:
                        skipped.append((row["name"], "; ".join(refused.messages)))
                        continue
                else:
                    reused += 1

                if not with_contracts or employee.contracts.exists():
                    continue
                try:
                    create_contract(
                        employee=employee,
                        actor=actor,
                        fixed_allowances=[],
                        contract_type=ContractType.PERMANENT,
                        start_date=employee.hire_date,
                        branch=branch,
                        job_title=row["job_title"],
                        department=row["department"],
                        wage_basis=WageBasis.MONTHLY,
                        basic_salary=Decimal(row["basic_salary"]),
                        scheduled_work_days=Decimal(options["work_days"]),
                        scheduled_hours=Decimal(options["hours_per_day"]),
                    )
                    contracts += 1
                except ValidationError as refused:
                    skipped.append((row["name"], "; ".join(refused.messages)))

            total = sum(Decimal(r["basic_salary"]) for r in payload["employees"])
            self.write("")
            self.write(f"=== موظفون · {organization.code} · {branch.code} ===")
            self.write(f"  موظفون : {made} أُنشئوا، {reused} موجودون")
            self.write(f"  عقود   : {contracts}")
            self.write(f"  مجموع الرواتب الأساسية: {total:,.0f} دينار")
            if skipped:
                self.write("")
                self.write("  لم يُنشأ:")
                for name, why in skipped:
                    self.write(f"    · {name} — {why}")
            if not with_contracts:
                self.write("")
                self.write("  بلا عقود. أضف --with-contracts مع أيام العمل وساعاتها.")

            if options["dry_run"]:
                self.write("")
                self.write("dry run — rolled back.")
                transaction.set_rollback(True)
