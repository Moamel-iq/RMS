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
    # Card takings between the sale and the acquirer's remittance (Task 4.0).
    # A clearing **asset** rather than cash: the money is real and the
    # restaurant does not have it yet, and treating a card sale as cash on hand
    # would make every cashier closing count short by that day's card volume.
    ("1-01-03", "شبكات الدفع", "Card settlement"),
    ("1-01-03-001", "تسوية مبيعات البطاقات", "Card Clearing"),
    ("1-02", "الذمم المدينة", "Receivables"),
    ("1-02-01", "ذمم تطبيقات التوصيل", "Delivery application receivables"),
    ("1-02-01-001", "ذمم بالي", "Bally Receivable"),
    ("1-02-01-002", "ذمم توترز", "Toters Receivable"),
    ("1-02-01-003", "ذمم طلبات", "Talabat Receivable"),
    # The organization default for `DELIVERY_APP_RECEIVABLE` (Task 4.0). A
    # general leaf rather than one of the three named applications above,
    # because the default has to be an account that is correct for an
    # application nobody has configured yet — and pointing it at Bally would
    # quietly file a new application's debt under a company that is not owed
    # it. Each application may override to its own account.
    ("1-02-01-009", "ذمم تطبيقات التوصيل — عام", "Delivery App Receivable — General"),
    # Inventory control and in-transit (Task 1.3). Accounts only: which of
    # them carries INVENTORY_CONTROL is an OrganizationAccountMapping the
    # organization records deliberately, never something this seed decides.
    ("1-03", "المخزون", "Inventory"),
    ("1-03-01", "مخزون المواد", "Materials inventory"),
    ("1-03-01-001", "مخزون المواد والسلع", "Inventory Control"),
    ("1-03-02", "بضاعة بالطريق", "Goods in transit"),
    ("1-03-02-001", "بضاعة بالطريق", "Goods in Transit"),
    # A payment's unallocated remainder (Task 2.15, PRC-055). An asset — cash
    # handed over before an invoice exists to net it against — and never a
    # negative payable, which would make the aging report lie about both.
    ("1-04", "السلف والدفعات المقدمة", "Advances and prepayments"),
    ("1-04-01", "سلف الموردين", "Supplier advances"),
    ("1-04-01-001", "سلف الموردين", "Supplier Advances"),
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
    # Contra-revenue, and deliberately in class 4 rather than class 6 (Task
    # 4.0). A restaurant-funded discount is money the restaurant chose not to
    # collect: it reduces what the restaurant earns. Booking it as a marketing
    # expense would leave gross revenue overstated and marketing overstated by
    # the same amount, and both figures would look defensible on their own.
    #
    # An **application**-funded discount never reaches this account. The
    # application reimburses it, so it is part of what the application owes.
    ("4-02", "خصومات المبيعات", "Sales discounts"),
    ("4-02-01", "خصومات ممولة من المطعم", "Restaurant-funded discounts"),
    ("4-02-01-001", "خصومات المبيعات المموّلة من المطعم", "Restaurant-funded Sales Discount"),
    # Separate from discounts because the two answer different questions. A
    # discount is a pricing decision made before the sale; a return is a sale
    # that stopped being one afterwards. Netting them would make a month of
    # generous promotions indistinguishable from a month of rejected food.
    ("4-03", "مردودات المبيعات", "Sales returns"),
    ("4-03-01", "مردودات وإلغاءات", "Returns and cancellations"),
    ("4-03-01-001", "مردودات وإلغاءات المبيعات", "Sales Returns and Cancellations"),
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
    # Stock destroyed in a warehouse (Task 1.6). An operating expense and never
    # a cost of sales: spoiled food was not sold, and burying it in food cost
    # would flatter the gross margin by exactly the amount that was thrown
    # away. Class 6 makes a cost centre mandatory, which is the control — waste
    # nobody's kitchen carries is waste nobody reduces.
    ("6-02-01-002", "هالك المخزون", "Inventory Waste Expense"),
    # What the delivery applications take (Task 4.0). Accrued at the sale from
    # the effective agreement, not discovered at settlement: the rate is known
    # the day the order is taken, and waiting for a statement would leave a
    # month's margin unknown until the following month.
    #
    # Two leaves rather than one, because a percentage of value and a fixed fee
    # per order behave differently as volume moves, and a single account would
    # hide which of the two changed.
    ("6-03", "مصروفات البيع والتوصيل", "Selling and delivery expenses"),
    ("6-03-01", "عمولات ورسوم التطبيقات", "Application commissions and fees"),
    ("6-03-01-001", "عمولات تطبيقات التوصيل", "Delivery Commission Expense"),
    ("6-03-01-002", "رسوم تطبيقات التوصيل الأخرى", "Delivery Other Fee Expense"),
    # 7 Other income and expense
    ("7", "إيرادات ومصروفات أخرى", "Other income and expense"),
    ("7-09", "فروقات وتسويات", "Differences and adjustments"),
    ("7-09-01", "تقريب النقد", "Cash rounding"),
    ("7-09-01-001", "أرباح وخسائر تقريب النقد", "Cash Rounding Gain/Loss"),
    # The two **bidirectional** inventory difference accounts (Task 1.6). Each
    # takes a debit when the books were too high and a credit when they were
    # too low, so neither is an expense account in the ordinary sense and
    # neither belongs in class 6: a count that finds more rice than expected is
    # not negative spending. One account per direction was considered and
    # rejected — the pair would have to be netted in every report that asks the
    # only interesting question, which is what the variance came to.
    ("7-09-02", "فروقات الجرد", "Count variance"),
    ("7-09-02-001", "فروقات الجرد", "Inventory Count Variance"),
    ("7-09-03", "تسويات المخزون", "Inventory adjustments"),
    ("7-09-03-001", "تسويات المخزون", "Inventory Adjustment"),
    # The difference between the book value that left on a supplier return and
    # the credit the supplier allows (Task 2.13 seeds it, Task 2.14 posts it).
    # Class 7 beside the other bidirectional difference accounts, and for the
    # same reason they are here: a return that credits more than it removed is
    # a gain and one that credits less is a loss, both are real, and neither is
    # negative spending. ADR-022 says both belong where somebody can see them.
    #
    # Deliberately NOT the purchase price variance account. That figure is
    # invoice-versus-receipt on goods coming in; this one is credit-versus-book
    # value on goods going out. Merging them would hide a supplier's pricing
    # behaviour inside the arithmetic of having averaged two deliveries.
    ("7-09-04", "فروقات إرجاع المشتريات", "Purchase return variance"),
    ("7-09-04-001", "فروقات إرجاع المشتريات", "Purchase Return Variance"),
    # The two bidirectional Sales difference accounts (Task 4.0), here for the
    # same reason the count variance is: an application that over-remits and a
    # till that is over are not negative spending, and class 6 would make both
    # look like costs that went the wrong way.
    #
    # Neither is reached automatically. An unexplained settlement difference
    # blocks posting until somebody categorises it and states a reason — a
    # system that silently absorbs differences is one where a mis-configured
    # commission rate stays invisible for a year.
    ("7-09-05", "فروقات تسويات التطبيقات", "Delivery settlement variance"),
    ("7-09-05-001", "فروقات تسويات التطبيقات", "Delivery Settlement Variance"),
    ("7-09-06", "فروقات الصندوق", "Cash over and short"),
    ("7-09-06-001", "فروقات الصندوق", "Cash Over and Short"),
    # 8 Clearing and control
    ("8", "حسابات وسيطة ورقابية", "Clearing and control"),
    ("8-01", "حسابات وسيطة", "Clearing accounts"),
    ("8-01-01", "بين الفروع", "Inter-branch"),
    ("8-01-01-001", "حساب وسيط بين الفروع", "Inter-branch Clearing"),
    ("8-01-02", "وكالة ميم", "MEM agency"),
    ("8-01-02-001", "حساب وسيط وكالة ميم", "MEM Agency Clearing"),
    # Where an invoice-versus-receipt price difference is parked (Task 2.12).
    # A clearing account rather than an expense one, and deliberately so.
    #
    # Task 2.0 §15 proposed `5-02-01-001`. Class 5 sets `requires_cost_center`,
    # and a supplier invoice has no cost centre to give — the document belongs
    # to a branch, not a department. More importantly ADR-022 already rejects
    # posting the variance to cost of goods sold: it conflates a purchasing
    # outcome with a consumption outcome, and food cost would then move for
    # reasons that have nothing to do with the kitchen.
    #
    # So the difference is parked rather than classified. A later, explicitly
    # specified period-end process splits this balance between inventory still
    # on hand and cost of sales for what was consumed, taking its branch and
    # cost centre from inventory ownership — never from the supplier invoice.
    # Task 2.12 does not build that process, and until it exists this balance
    # is expected to be non-zero and is reconciled by invoice, match and
    # allocation rather than cleared.
    ("8-01-03", "فروقات أسعار المشتريات", "Purchase price variance"),
    ("8-01-03-001", "تسوية فروقات أسعار المشتريات", "Purchase Price Variance Clearing"),
    # Where the book value of goods sent back to a supplier waits for the
    # credit note that settles it (Task 2.13). A clearing account in the exact
    # sense: stock has left the warehouse and the supplier has not yet agreed
    # what it is worth, so there is a real claim in flight and no document
    # stating its amount. Task 2.14's credit note clears it.
    ("8-01-04", "مرتجعات الموردين", "Supplier returns"),
    ("8-01-04-001", "تسوية مرتجعات الموردين", "Supplier Return Clearing"),
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
