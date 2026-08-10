"""
Seed the restaurant chart of accounts and cost centres (ADR-014, ADR-015).

Deterministic reference data, not factories: reports must be reproducible.
Idempotent — safe to re-run.

Parents are created before children because an account derives its parent from
its own code, so the tree has to be built top down.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandError
from django.db import transaction

from apps.accounting.models import Account, CostCenter
from apps.accounting.services import create_account, create_cost_center
from apps.core.console import SeedCommand
from apps.organizations.models import Organization

#: code, Arabic, English. Ordered parents-first.
CHART: list[tuple[str, str, str]] = [
    # 1 Assets
    ("1", "الأصول", "Assets"),
    ("1-01", "النقد وما يعادله", "Cash and equivalents"),
    ("1-01-01", "الصناديق", "Cash boxes"),
    ("1-01-01-001", "الصندوق الرئيسي", "Main Cash"),
    ("1-01-02", "الحسابات البنكية", "Bank accounts"),
    ("1-01-02-001", "البنك", "Bank"),
    ("1-02", "الذمم المدينة", "Receivables"),
    ("1-02-01", "ذمم تطبيقات التوصيل", "Delivery application receivables"),
    ("1-02-01-001", "ذمم بالي", "Bally Receivable"),
    ("1-02-01-002", "ذمم توترز", "Toters Receivable"),
    ("1-02-01-003", "ذمم طلبات", "Talabat Receivable"),
    # Inventory control and in-transit (Task 1.3). Accounts only: which of
    # them carries INVENTORY_CONTROL is an OrganizationAccountMapping the
    # organization records deliberately, never something this seed decides.
    ("1-03", "المخزون", "Inventory"),
    ("1-03-01", "مخزون المواد", "Materials inventory"),
    ("1-03-01-001", "مخزون المواد والسلع", "Inventory Control"),
    ("1-03-02", "بضاعة بالطريق", "Goods in transit"),
    ("1-03-02-001", "بضاعة بالطريق", "Goods in Transit"),
    # 2 Liabilities
    ("2", "الالتزامات", "Liabilities"),
    ("2-01", "الذمم الدائنة", "Payables"),
    ("2-01-01", "ذمم الموردين", "Supplier payables"),
    ("2-01-01-001", "ذمم الموردين", "Accounts Payable"),
    # Goods physically received that no supplier invoice has caught up with.
    # A clearing liability, not a payable: nobody is owed a stated amount
    # until the invoice arrives, and Procurement clears this against it.
    ("2-01-02", "بضاعة مستلمة غير مفوترة", "Goods received not invoiced"),
    ("2-01-02-001", "بضاعة مستلمة غير مفوترة", "Goods Received Not Invoiced"),
    # 3 Equity
    ("3", "حقوق الملكية", "Equity"),
    ("3-01", "رأس المال", "Capital"),
    ("3-01-01", "رأس المال", "Capital"),
    ("3-01-01-001", "رأس المال", "Owner Capital"),
    ("3-02", "أرصدة افتتاحية", "Opening balances"),
    ("3-02-01", "أرصدة افتتاحية", "Opening balances"),
    ("3-02-01-001", "أرصدة افتتاحية - مخزون", "Inventory Opening Equity"),
    # 4 Revenue
    ("4", "الإيرادات", "Revenue"),
    ("4-01", "إيرادات المبيعات", "Sales revenue"),
    ("4-01-01", "المبيعات المباشرة", "Direct sales"),
    ("4-01-01-001", "مبيعات الصالة", "Dine-in Sales"),
    ("4-01-01-002", "مبيعات السفري", "Takeaway Sales"),
    ("4-01-02", "مبيعات التطبيقات", "Delivery application sales"),
    ("4-01-02-001", "مبيعات تطبيقات التوصيل", "Delivery App Sales"),
    # 5 Cost of sales
    ("5", "كلفة المبيعات", "Cost of sales"),
    ("5-01", "كلفة المواد", "Material cost"),
    ("5-01-01", "كلفة الأغذية", "Food cost"),
    ("5-01-01-001", "كلفة الأغذية", "Food COGS"),
    # Consumption destinations. Separate leaves because what a thing is
    # consumed *as* is what makes the figure useful: ingredients, packaging,
    # and cleaning materials answer different questions about the same
    # kitchen. Which item maps to which is an organization decision.
    ("5-01-02", "استهلاك المواد", "Materials consumption"),
    ("5-01-02-001", "استهلاك المواد الغذائية", "Food Materials Consumed"),
    ("5-01-02-002", "استهلاك مواد التغليف", "Packaging Materials Consumed"),
    ("5-01-02-003", "استهلاك المواد الاستهلاكية", "Consumables Consumed"),
    # 6 Operating expenses
    ("6", "المصروفات التشغيلية", "Operating expenses"),
    ("6-01", "المصروفات الإدارية", "Administrative expenses"),
    ("6-01-01", "الرواتب", "Salaries"),
    ("6-01-01-001", "الرواتب", "Salaries"),
    ("6-01-02", "الإيجار", "Rent"),
    ("6-01-02-001", "الإيجار", "Rent"),
    # Stock that left one branch and never reached the other (Task 1.5). An
    # operating expense rather than a cost-of-sales line, because nothing was
    # sold: the goods were lost. Its class makes a cost centre mandatory, which
    # is exactly right — a loss nobody's department carries is a loss nobody
    # investigates.
    ("6-02", "خسائر تشغيلية", "Operating losses"),
    ("6-02-01", "خسائر المخزون", "Inventory losses"),
    ("6-02-01-001", "عجز التحويلات", "Transfer Shortage Loss"),
    # 7 Other income and expense
    ("7", "إيرادات ومصروفات أخرى", "Other income and expense"),
    ("7-09", "فروقات وتسويات", "Differences and adjustments"),
    ("7-09-01", "تقريب النقد", "Cash rounding"),
    ("7-09-01-001", "أرباح وخسائر تقريب النقد", "Cash Rounding Gain/Loss"),
    # 8 Clearing and control
    ("8", "حسابات وسيطة ورقابية", "Clearing and control"),
    ("8-01", "حسابات وسيطة", "Clearing accounts"),
    ("8-01-01", "بين الفروع", "Inter-branch"),
    ("8-01-01-001", "حساب وسيط بين الفروع", "Inter-branch Clearing"),
    ("8-01-02", "وكالة ميم", "MEM agency"),
    ("8-01-02-001", "حساب وسيط وكالة ميم", "MEM Agency Clearing"),
]

#: The account the cash settlement rounding policy will post its difference to
#: (ADR-012). Seeded even though CASH_ROUNDING_ENABLED is False, so enabling it
#: later fails loudly if it is missing rather than mid-settlement.
CASH_ROUNDING_ACCOUNT_CODE = "7-09-01-001"

COST_CENTERS: list[tuple[str, str, str]] = [
    ("KITCHEN", "المطبخ", "Kitchen"),
    ("HALL", "الصالة", "Hall"),
    ("WAREHOUSE", "المخزن", "Warehouse"),
    ("DELIVERY", "التوصيل", "Delivery"),
    ("ADMIN", "الإدارة", "Administration"),
    ("HR", "الموارد البشرية", "Human Resources"),
]


class Command(SeedCommand):
    help = "Seed the restaurant chart of accounts and cost centres for an organization."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--organization",
            required=True,
            help="Organization code, e.g. KM",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        code = options["organization"].strip().upper()
        organization = Organization.objects.filter(code=code).first()
        if organization is None:
            raise CommandError(f"No organization with code {code}.")

        created_accounts = 0
        for account_code, name_ar, name_en in CHART:
            if Account.objects.filter(organization=organization, code=account_code).exists():
                continue
            create_account(
                organization=organization,
                code=account_code,
                name_ar=name_ar,
                name_en=name_en,
            )
            created_accounts += 1

        created_centers = 0
        for center_code, name_ar, name_en in COST_CENTERS:
            if CostCenter.objects.filter(organization=organization, code=center_code).exists():
                continue
            create_cost_center(
                organization=organization, code=center_code, name_ar=name_ar, name_en=name_en
            )
            created_centers += 1

        if not Account.objects.filter(
            organization=organization, code=CASH_ROUNDING_ACCOUNT_CODE
        ).exists():
            raise CommandError(
                f"{CASH_ROUNDING_ACCOUNT_CODE} (Cash Rounding Gain/Loss) is missing. "
                "Cash settlement rounding cannot be enabled without it."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{organization.code}: {created_accounts} accounts and "
                f"{created_centers} cost centres created."
            )
        )
