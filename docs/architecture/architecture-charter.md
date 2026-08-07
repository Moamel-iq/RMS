KHAN MANDI RESTAURANT ERP
ARCHITECTURE, BUILD ORDER, CLAUDE CODE WORKFLOW, AND AI FEATURES

Good scope — these modules have a real dependency order, and getting that order wrong is one of the main reasons restaurant ERP systems turn into spaghetti. The system should be built as a modular Django monolith first, with clear domain boundaries, append-only ledgers, explicit posting rules, and complete vertical slices. Do not generate six partially connected applications at once. Build one dependable business capability at a time, prove its invariants with tests and real data, and only then allow the next module to depend on it.

The plan has four parts:

1. Architecture decisions that must be made before coding.
2. The correct module build order.
3. How to use Claude Code safely and effectively during implementation.
4. AI features that can be added after the transactional core is stable.


PART 1 — ARCHITECTURE DECISIONS BEFORE ANY CODE

1. Two append-only ledgers are the financial and operational spine

The system should have two authoritative ledgers:

Inventory ledger:
- StockMovement
- StockMovementLine, if one movement document contains multiple items

Accounting ledger:
- JournalEntry
- JournalLine

Purchases, receipts, supplier returns, transfers, kitchen issues, recipe consumption, waste, stock-count adjustments, sales cost recognition, payroll, supplier payments, application settlements, and other transactions should create entries in the relevant ledger or ledgers. Not every transaction writes to both ledgers: payroll normally writes only to accounting, while a warehouse transfer normally writes to inventory and may have no profit-and-loss effect. The posting policy decides what each business event creates.

Current stock, inventory value, supplier balances, customer or application receivables, cash balances, expense balances, and account balances are derived from posted ledger entries. They must not be maintained as unrelated mutable numbers.

Business documents may be editable while they are in DRAFT or, where permitted, APPROVED status. Once a document is POSTED, its financial or inventory effects are immutable. A posted transaction must not be silently edited or deleted. Corrections follow this pattern:

Original posted transaction
→ Reversal transaction
→ Correct replacement transaction

A standard document lifecycle should be used consistently:

DRAFT
→ SUBMITTED
→ APPROVED
→ POSTED
→ REVERSED

Not every document needs every state, but the meaning of each state must be consistent. “Deleted” must never be used as a substitute for reversing a posted financial transaction.

For performance, the system may maintain stock-balance snapshots, account-balance snapshots, daily aggregates, or materialized reporting views. Those are projections or caches, not independent sources of truth. They must be rebuildable from the ledgers and should include a last-posted movement or journal reference so their freshness can be verified.


2. Business documents and ledgers are different things

Do not model a purchase as one editable form that simultaneously means ordering, receiving, invoicing, and paying. These are separate business events and often happen on different dates.

The procurement lifecycle should distinguish:

Purchase Order
- What was requested from the supplier.
- Does not increase stock and does not create a payable.

Goods Receipt
- What physically entered the warehouse.
- Increases stock on the receipt date.
- Records accepted quantity, rejected quantity, batch or expiry information where relevant, and warehouse destination.

Supplier Invoice
- What the supplier charged.
- Creates or confirms the supplier payable.
- May reference one or more receipts.
- Can include freight, delivery, discounts, or other landed-cost components.

Supplier Payment
- What was paid, by which cashbox or bank account, and when.
- Reduces cash or bank and reduces the supplier payable.
- Must support partial payments and allocation to the oldest or selected invoices.

Supplier Return
- What was physically returned.
- Reduces stock and creates the proper accounting adjustment or supplier credit.

For example, meat can arrive on one day, the invoice can be approved two days later, and payment can occur under a monthly credit agreement. The stock date, liability date, and payment date are not necessarily the same and should not be forced into one timestamp.

The same separation applies elsewhere:

Sales order or daily sales import
≠ payment
≠ delivery-application settlement
≠ refund

Attendance record
≠ payroll calculation
≠ payroll approval
≠ salary payment


3. Units of measure require an item-specific conversion layer from day one

You may buy rice in a 30 kg sack, store it in kilograms, and consume it in recipes by grams. You may buy oil by carton, count bottles, and consume liters or milliliters. Retrofitting this after transactions exist is a rewrite.

Each stock item should have one base stock unit. All ledger quantities are normalized to that base unit.

Example:

Item: Rice 272
Base stock unit: kilogram

Allowed purchasing or operational units:
- Sack 30 kg
- Sack 50 kg
- Gram
- Kilogram

A conversion such as “one sack equals 30 kilograms” is usually item-specific. One carton of cups, one carton of chicken, and one carton of oil do not contain the same quantity. Therefore, avoid a global assumption that “carton” always has one conversion factor.

Separate three concepts:

A. Unit conversion
- 1 kilogram = 1,000 grams
- 1 liter = 1,000 milliliters

B. Packaging conversion
- 1 rice sack = 30 kilograms
- 1 chicken carton = 10 pieces

C. Yield or production transformation
- 10 kg raw meat becomes 8.7 kg usable meat after trimming
- One batch of sauce produces 42 portions
- One whole chicken produces two half portions

Yield is not a unit conversion. It is a production result with possible loss, variance, and cost allocation.

Every conversion should define:
- Item
- From unit
- To base unit
- Conversion factor
- Effective dates if packaging changes
- Whether fractional quantities are allowed
- Minimum issue or purchase increment
- Rounding policy


4. Use Decimal for every monetary, quantity, rate, and cost calculation

Never use binary floating-point values for:

- Money
- Unit cost
- Inventory quantity
- Recipe quantity
- Commission rate
- Discount rate
- Payroll rate
- Yield percentage
- Exchange rate, if additional currencies are introduced

Use integers for IDs and truly whole counts where fractions are impossible. Use Decimal for counts such as half chickens, fractional kilograms, liters, or prorated salary calculations.

Create one written precision and rounding policy before implementing financial services. It should define:

- Storage precision for quantities
- Storage precision for unit costs
- Storage precision for percentages and rates
- Posting precision for IQD
- Display precision
- Rounding mode
- Whether line values or only document totals are rounded
- How residual rounding differences are allocated
- How application commissions and shared discounts are rounded
- How payroll proration is rounded

Do not let each module make its own rounding decision. Round only at defined business boundaries. Intermediate costing calculations normally require more precision than the final posted IQD amount.


5. Adopt moving weighted-average costing, but define the whole policy

Moving weighted-average costing is simpler than FIFO and is appropriate for a restaurant ERP, but the sentence “use weighted average” is not a complete design.

The average-cost scope should normally be:

Organization
+ Branch
+ Warehouse
+ Item

A receipt updates the average cost using the existing quantity and value plus the new received quantity and landed value. The exact formula, precision, and rounding must be documented and tested.

The costing policy must explicitly answer:

- Is negative stock prohibited?
- What happens when a backdated receipt is posted?
- How are supplier returns valued?
- How are warehouse and inter-branch transfers valued?
- How are freight, delivery, and other landed costs allocated?
- How are price corrections handled after a receipt is consumed?
- What cost is used for waste and stock adjustments?
- How are opening quantities and opening values loaded?
- Can a closed period be recalculated?
- How are zero-quantity residual values corrected?
- How is inventory revaluation recorded?
- What happens when a receipt is entered with zero or missing cost?

For this system, normal posting should prohibit negative stock. Allowing consumption before a valid receipt exists makes moving-average costing unreliable and can create impossible or unstable values.

Backdated transactions must not silently rewrite history. Define a controlled policy: reject postings into locked periods, require period reopening by an authorized role, or run an explicit revaluation process that creates traceable adjustment entries.


6. Recipe costing must distinguish theoretical consumption from actual consumption

A restaurant system needs more than a table that lists ingredients per dish.

Each recipe should support:

- Recipe version
- Effective-from and effective-to dates
- Branch applicability
- Batch size
- Expected output quantity
- Portion size
- Ingredient quantity and unit
- Preparation loss
- Cooking yield
- Optional ingredients
- Substitute ingredients
- By-products, where relevant
- Approval status
- Cost snapshot or cost-calculation date
- Notes and preparation instructions

Historical sales must use the recipe version that was effective when the item was sold. A recipe changed in September must not silently change the theoretical cost of July sales.

The system should calculate two different kinds of consumption.

Theoretical consumption:
Quantity sold
× active recipe quantity per serving
= expected ingredient consumption

Example:

40 Chicken Mandi portions sold
× rice, spices, oil, chicken, and packaging per portion
= expected consumption

Actual consumption:
Warehouse issues to kitchen
+ production usage
+ recorded waste
+ stock adjustments
+ transfers into the kitchen
− returns from the kitchen
= actual consumption

Variance:
Actual consumption
− theoretical consumption
= usage variance

This variance is one of the most valuable restaurant controls because it can reveal:

- Over-portioning
- Recipe noncompliance
- Unrecorded waste
- Theft
- Incorrect counts
- Incorrect conversion factors
- Incorrect recipe quantities
- Production-yield problems

The MVP may use controlled backflush consumption, where approved sales generate recipe consumption automatically. If that approach is chosen, it must be written down as an explicit simplification, and waste, staff meals, complimentary meals, production batches, and manual corrections must still be recorded separately.


7. Sales and delivery-application settlements are a first-class module

Sales cannot be omitted from the architecture. It connects recipes, inventory consumption, application receivables, cash reconciliation, profitability, and accounting.

The Sales module should support:

- Daily sales by menu item
- Dine-in
- Takeaway
- Direct delivery
- Delivery applications
- Cash
- Card
- Application receivable
- Other payment methods
- Gross menu value
- Restaurant-funded discount
- Application-funded discount
- Commission
- Delivery fee
- Refund
- Void
- Complimentary meal
- Staff meal
- Promotion
- Service charge, if used
- Tax, if applicable
- Net amount due from each application
- Settlement and settlement variance

For Khan Mandi, sales channels include applications such as Bally, Talabat, Toters, Talabaty, Mazajk, and Ala Al-Saree’, as well as direct delivery, takeaway, and dine-in. Each channel can have its own effective-dated commercial agreement:

- Commission percentage
- Restaurant share of discounts
- Application share of discounts
- Fixed fees
- Settlement cycle
- Withholding or adjustment rules
- Activation and termination dates

Do not store only “net application sales.” Preserve every component separately so the result can be reconciled:

Gross order value
− restaurant-funded discount
− commission
− application charges
± adjustments
= amount receivable from the application

An application-funded discount may not reduce restaurant revenue under the same rule as a restaurant-funded discount. The accounting treatment must follow the actual contract, so both values must remain separate.

Application settlement is its own event:

Expected receivable
− cash actually received
= settlement variance

The system should allow one settlement to cover many orders or daily summaries and should keep unresolved differences visible until reconciled.

Sales can begin with controlled end-of-day manual entry by item, as required by the MVP. The design should still leave a clear import boundary for future POS or application integrations.


8. Build a thin accounting kernel early, then expand the full accounting module later

A small accounting posting engine should exist during Foundations so every subsequent module integrates correctly from the beginning.

The core service may conceptually look like:

post_entry(posting_request)

However, it must do more than check that debits equal credits. It should validate and record:

- Organization
- Branch
- Accounting date
- Document date
- Fiscal period
- Source document type
- Source document ID
- Unique idempotency key
- Journal number
- Debit and credit lines
- Account
- Cost center or operational dimension
- Currency and exchange rate, if applicable
- Narration
- Created by
- Approved by
- Posted at
- Reversal reference
- Posting rule version
- Entry status

Mandatory accounting invariants include:

- Total debits equal total credits
- Posted entries are immutable
- Closed periods reject new postings
- Duplicate retries do not create duplicate entries
- Reversals point to the original entry
- Every line belongs to the same organization and permitted branch
- Control accounts are not posted manually unless specifically authorized
- Sequential document numbering follows the defined scope
- The source document and journal entry remain consistently linked

Domain modules should not scatter raw account IDs through their code. A better flow is:

Business event:
SupplierInvoicePosted

Accounting posting policy:
Debit Inventory or Expense
Credit Accounts Payable

Business event:
ApplicationSettlementReceived

Accounting posting policy:
Debit Bank or Cash
Debit Commission Expense, where appropriate
Credit Application Receivable
Post any settlement difference according to an approved rule

The domain event expresses what happened. Accounting owns the mapping to accounts. This keeps financial policy in one place and makes chart-of-account changes manageable.

The business document, stock movement, journal entry, and posting status should succeed or fail as one database transaction wherever they are part of one synchronous operation. Never allow a supplier invoice to show POSTED while its journal entry failed, or a kitchen issue to reduce stock without its corresponding document reaching the correct state.


9. Model organization, branch, warehouse, kitchen, and cash points explicitly

Do not add a Branch foreign key blindly to every table.

Use a hierarchy such as:

Organization
└── Branch
    ├── Warehouse
    ├── Kitchen or production location
    ├── Cash point
    └── Operational cost centers

Some records are organization-level or global:

- Units of measure
- Currencies
- Permission definitions
- Shared item master, if centrally managed
- Shared supplier master, if centrally managed
- Account templates
- Sales-channel definitions

Other records are branch-specific:

- Stock movements
- Warehouse balances
- Daily sales
- Cash sessions
- Goods receipts
- Attendance
- Payroll runs
- Application receivables
- Branch expenses

An inter-branch or inter-warehouse transfer requires both source and destination. One generic branch field is not enough.

Use database constraints and service-layer authorization so that:
- A branch user cannot post into another branch without permission.
- Uniqueness is scoped correctly by organization or branch.
- Reports do not leak data between branches.
- Cross-branch transactions require explicit roles and approvals.


10. Treat the restaurant business day separately from the calendar date

Khan Mandi operates past midnight. A sale recorded at 1:30 AM may belong to the previous restaurant operating day, not the new calendar day.

Store both:

- Exact timezone-aware timestamp
- Business date or operating date

Define the business-day cutoff explicitly, for example:

Business day starts at 09:00
Business day closes after the final shift and reconciliation, potentially around 03:00 the next calendar day

The exact rule should be configurable by branch. It affects:

- Daily sales
- Cashier shifts
- Application sales
- Stock consumption
- Attendance
- Daily closing
- Management reports
- Period-end reconciliation

Do not calculate the business date differently in each module. Provide one tested domain service that maps a timestamp and branch schedule to the correct operating date.


11. Audit history, approvals, and access control are non-negotiable

A model-history package can help record changes, but it is not the complete financial audit mechanism.

Critical records should also capture:

- Actor
- Approver
- Timestamp
- Previous state
- New state
- Reason
- Source document
- Request or correlation ID
- Import batch
- Reversal reference
- Failed posting attempt
- Period reopening action
- Permission override, where allowed

Avoid bulk updates on financially important records unless they go through an audited service designed for that operation.

Use role-based permissions and separation of duties. Examples:

- Storekeeper records a receipt.
- Purchasing or authorized manager approves it.
- Accountant posts or reviews the financial result.
- Cashier enters closing data.
- Manager approves material variances.
- Only an authorized finance role can reopen a period.
- No user should approve their own high-risk transaction unless an explicit exception policy allows it.

The exact approval thresholds can remain simple for the MVP, but the state model must not block stronger controls later.


PART 1B — RECOMMENDED BUILD ORDER

Build vertical slices, not horizontal layers. A module is not complete because its models exist. Each module should ship with its specification, models, migrations, services, permissions, tests, API or UI, admin support, posting integration, audit behavior, import or seed path, and reconciliation report before the next module relies on it.

Phase 0 — Foundations

Scope:
- Project structure
- Organization and branch
- Users and roles
- Branch-scoped authorization
- Timezone and restaurant business date
- Units of measure
- Item and account naming conventions
- Document states and numbering
- Decimal and rounding policy
- Audit context
- Thin accounting journal engine
- Common posting, reversal, approval, and idempotency primitives
- Arabic and English localization foundation

Why first:
Every other module depends on these rules. Retrofitting branch scope, business dates, units, document states, or idempotency later is expensive and risky.

Phase 1 — Inventory Core

Scope:
- Item master
- Categories
- Warehouses and kitchen locations
- Base units and item conversions
- Opening balances
- Receipts as inventory events
- Issues
- Transfers
- Returns
- Waste
- Stock counts
- Adjustments
- Stock ledger
- Stock on hand
- Moving weighted-average valuation
- Reorder levels
- Inventory reconciliation

Why here:
Everything operational writes to inventory. The ledger, unit conversions, and costing behavior must be dependable before purchases, recipes, or sales consume them.

Phase 2 — Suppliers, Procurement, and Accounts Payable Integration

Scope:
- Supplier master
- Purchase orders
- Goods receipts
- Supplier invoices
- Landed costs
- Supplier returns and credit notes
- Payments
- Outstanding balances
- Payment allocation
- Credit terms
- Posting policies
- Receipt-to-invoice reconciliation

Why here:
This is the first real source of stock quantity and cost. Recipe costing is meaningless until purchase cost and receipt timing are reliable.

Phase 3 — Recipes, Kitchen, Production, and Costing

Scope:
- Recipe versions
- Batch recipes
- Portion recipes
- Preparation and production batches
- Yield and loss
- Kitchen issues and returns
- Waste
- Staff meals
- Complimentary meals
- Theoretical consumption
- Actual consumption
- Usage variance
- Plate cost
- Historical cost snapshot
- Menu-item mapping

Why here:
This module consumes inventory and uses its weighted-average costs. It creates the bridge between stock and menu profitability.

Phase 4 — Sales, Channels, Discounts, and Settlements

Scope:
- Menu items
- Daily manual sales entry
- Dine-in, takeaway, and delivery
- Delivery applications
- Effective-dated commissions
- Restaurant-funded and application-funded discounts
- Refunds and voids
- Payment methods
- Application receivables
- Application settlements
- Cashier closing
- Daily reconciliation
- Sales-driven recipe consumption or theoretical consumption
- Channel profitability

Why here:
Sales completes the operational cycle and provides the quantities required for recipe consumption, margin analysis, receivables, and daily control.

Phase 5 — Full Accounting and Treasury

Scope:
- Chart of accounts
- Account types and hierarchy
- Journal review and posting
- Cashboxes and bank accounts
- Supplier payables
- Application receivables
- Expenses
- Accruals and prepayments
- Franchise or agency fees
- Period closing and locking
- Trial balance
- General ledger
- Profit and loss
- Balance sheet
- Cash flow mapping
- Reconciliation workflows

Why here:
The journal engine already exists, and phases 1–4 have created real posting requirements. The full accounting model can now be validated against actual operations rather than designed in isolation.

Phase 6 — HR, Attendance, Advances, and Payroll

Scope:
- Employee master
- Contracts and compensation
- Shifts
- Attendance
- Leave and absence
- Overtime
- Deductions
- Advances and employee receivables
- Payroll calculation
- Payroll approval
- Salary release
- Payroll payment
- Accounting postings
- Employee statements

Why here:
HR is comparatively isolated, but payroll must use the finished accounting, approval, period, and payment primitives rather than creating a second financial system.

Phase 7 — Reports, Closing, and Management Controls

Scope:
- Inventory movement and valuation
- Slow or fast-moving items
- Count variance
- Purchase-price trends
- Recipe cost
- Theoretical versus actual usage
- Item and channel profitability
- Application settlement aging
- Daily sales and cashier reconciliation
- Payroll summaries
- Supplier aging
- General ledger and financial statements
- Branch KPI dashboard
- Month-end close checklist

Why here:
Reports are read models over validated transactions. Diagnostic reports should be created during every phase, but the complete management-reporting layer should sit on stable ledgers and defined metric rules.

Phase 8 — Controlled AI Features

Scope:
- Supplier invoice extraction
- Item matching suggestions
- Natural-language reporting over approved read models
- Variance explanations
- Data-quality warnings
- Human approval and audit of every AI-assisted financial action

Why last:
AI should accelerate a stable process, not hide an undefined one.


FOUNDATIONS CHECKLIST

Use a supported Django LTS release and a compatible supported Python version rather than choosing a version only because it is newest. Use PostgreSQL as the source database.

A suitable technical shape for this project is:

- Django modular monolith
- Django Ninja and Pydantic for the API, matching the existing team’s experience
- Django ORM
- PostgreSQL
- Service layer for commands and business transactions
- Selector or query layer for complex reads and reports
- Explicit posting-policy layer for accounting
- pytest-django
- factory_boy
- Hypothesis for high-value calculation and state-machine tests
- A task queue for imports, report generation, scheduled closing jobs, and other long-running work
- Structured logging
- Error monitoring
- Docker Compose for repeatable local development
- CI for linting, migrations, type checks, and tests
- Arabic and English i18n
- Proper RTL frontend design
- Effective-dated business rules
- Database constraints in addition to Python validation
- Idempotency keys on imports and external events

Django REST Framework versus Django Ninja is an API decision. HTMX versus React is a frontend decision. They are not direct alternatives. Keep those choices separate.

For this project, the initial recommendation is:

Backend API:
Django Ninja + Pydantic

Business logic:
Service layer with explicit command functions

Read logic:
Selector/query services and reporting views

Frontend:
Choose separately between Django templates + HTMX or a React frontend based on the actual UI and team capacity

Do not put core accounting and costing rules in serializers, views, signals, or admin methods. Signals may be useful for non-critical notifications, but they should not be the hidden engine that posts stock or money.


PART 2 — HOW TO USE CLAUDE CODE FOR THIS PROJECT

Step 1 — Use Claude Code inside the actual repository

Chat is useful for architecture discussion. Implementation should happen with an agent that can inspect the real repository, edit files, run tests, examine migrations, and review diffs.

Create a CLAUDE.md at the repository root. Keep it concise and authoritative. It should include:

- The SRS is the authoritative business source.
- The approved module order.
- The append-only ledger rule.
- Posted records are corrected by reversal, never direct editing.
- Decimal only for money, quantities, costs, and rates.
- The moving weighted-average costing policy.
- Negative stock policy.
- Organization and branch boundaries.
- Restaurant business-date rule.
- Service-layer and selector-layer conventions.
- Django Ninja API conventions.
- Naming and folder conventions.
- Required commands for setup, tests, linting, and migrations.
- “Every money-touching or stock-touching function requires tests.”
- “No critical posting logic in signals.”
- “No destructive command, data deletion, migration reset, or force push without explicit approval.”
- “Do not implement a later phase before the current phase passes its definition of done.”

CLAUDE.md should contain stable project rules. Longer procedures should live in project documentation or reusable Claude skills rather than turning the root file into a large manual.


Step 2 — Make the SRS authoritative and create traceability before implementation

Claude must read the complete SRS and existing architecture documents before making a technical decision.

Create:

docs/requirements/traceability.md
docs/specs/
docs/adr/
docs/accounting/posting-rules/
docs/testing/golden-cases/

The traceability matrix should map:

Requirement ID
→ business rule
→ model
→ service
→ endpoint or UI
→ posting rule
→ test
→ implementation status

Architecture Decision Records should capture decisions that would be expensive to reverse, such as:

- Moving weighted average rather than FIFO
- Negative stock policy
- Business-day cutoff
- Branch costing scope
- Recipe versioning
- Inventory backflush versus manual kitchen issue
- Application discount recognition
- Period-lock behavior
- PDF-rendering engine
- API and frontend approach

Claude should ask questions only where the SRS is silent, contradictory, or insufficient. It should not repeatedly ask for decisions already documented.


Step 3 — Write a module specification before code

For each module, first create a written specification covering:

- Goals and non-goals
- Entities and relationships
- Document states
- Commands
- Queries
- Invariants
- Permissions
- Approval flow
- Posting events
- Failure behavior
- Idempotency
- Reversal behavior
- Concurrency risks
- Edge cases
- Imports
- Reconciliation
- Reports
- Acceptance tests

A strong prompt shape is:

“You are helping me design the Inventory module for a production restaurant ERP in Django. First read the SRS, architecture decisions, CLAUDE.md, and the existing repository. Do not write implementation code yet. Identify only unresolved business gaps. Examine waste, staff meals, complimentary meals, kitchen issues and returns, inter-warehouse and inter-branch transfers, supplier returns, stock counts, damaged goods, unit conversions, yield, negative stock, backdated entries, opening balances, and reversal behavior. Ask one unresolved question at a time. When the business rules are complete, write docs/specs/inventory.md, update the requirements traceability matrix, propose any required ADRs, list all invariants, and produce acceptance-test scenarios. Do not implement until the specification is internally consistent.”

The interrogation is one of the highest-value uses of the model. Restaurant inventory contains edge cases that often appear only after several months of operation. The model should surface them before the database is populated.


Step 4 — Implement one complete vertical slice at a time

For a transaction such as Stock Transfer, implement it end to end:

- Model
- Database constraints
- Migration
- Domain command
- Validation
- Permission
- Atomic posting
- Stock movements
- Accounting effect, if any
- Reversal
- API
- Admin or operational UI
- Unit tests
- Integration tests
- Concurrency tests
- Audit log
- Documentation
- Reconciliation query

Do not generate every model first, every serializer second, and every view third. That creates broad incomplete layers with no proven workflow.

A module is complete only when real sample data can enter, post, reverse, reconcile, and appear correctly in its reports.


Step 5 — Test first for anything touching money, stock, payroll, or settlement

Have Claude write the invariant test, run it and confirm that it fails for the expected reason, then implement the minimum correct behavior.

Core examples:

- Every journal entry balances.
- Stock on hand equals the sum of posted stock movements.
- Inventory value reconciles to quantity and cost according to policy.
- One unit conversion path produces the expected base quantity.
- A recipe cost equals the sum of its effective component costs.
- Historical sales use the correct recipe version.
- Application receivable equals the defined gross, discount, commission, and adjustment components.
- A partial supplier payment leaves the correct open balance.
- A mid-month salary matches a hand-calculated golden example.
- A reversal restores the expected state without deleting history.
- Posting the same idempotency key twice creates only one transaction.
- A failed journal posting rolls back the source document and stock effect.
- Two simultaneous issues cannot consume the same available stock beyond the allowed policy.
- A closed period rejects backdated postings.
- A sale after midnight maps to the correct restaurant business date.

Use hand-calculated golden examples for critical accounting and costing cases. Property-based testing is valuable for conversion, costing, balancing, and sequence-dependent inventory behavior. Stateful tests can generate sequences such as receive, transfer, consume, count, reverse, and receive again to discover combinations a few hand-written examples may miss.


Step 6 — Review AI output at the diff and business-rule level

Use small commits with one concern each.

Claude is often good at:
- CRUD
- Schemas
- Admin configuration
- Repetitive tests
- Migrations
- API wiring
- Import scaffolding
- Documentation
- Refactoring under strong tests

Review more heavily:
- Accounting signs
- Debit and credit direction
- Rounding
- Unit conversions
- Cost allocation
- Recipe yield
- Commission rules
- Discount funding
- Business-date mapping
- Period locks
- Reversal behavior
- Concurrency
- Authorization
- Data migrations

The dangerous failure mode is not usually invalid Python. It is clean, plausible code that implements the wrong business rule.

Before accepting a change, review:
- The specification
- The diff
- The migration
- The SQL or query plan for heavy reports
- The tests
- The journal and stock examples
- The rollback path
- The reconciliation result


Step 7 — Migrate Excel data through audited, repeatable commands

Existing employee, payroll, item, supplier, and recipe workbooks are valuable seed and migration sources.

Have Claude create Django management commands that:

- Read with openpyxl
- Validate the workbook structure
- Support dry-run mode
- Produce row-level error reports
- Normalize Arabic and English names carefully
- Map units explicitly
- Detect duplicates
- Use idempotency keys
- Avoid creating partial data on failure
- Produce before-and-after reconciliation totals
- Save an import-batch record
- Keep the source filename and checksum
- Allow safe re-running
- Require approval before posting opening balances or financial effects

For inventory opening balances, reconcile:

Total imported quantity by item
Total imported value by item
Grand inventory value
Rejected rows
Unmapped units
Duplicate items

For payroll, reconcile:

Employee count
Total basic salary
Total allowances
Total deductions
Total net pay

Do not let a spreadsheet import write directly to mutable stock or account-balance fields. It should create approved opening or migration transactions through the same posting services used by normal operations.


Step 8 — Decide Arabic, RTL, and PDF rendering early

Use Django i18n for interface strings. Consider bilingual fields such as name_ar and name_en for items, accounts, suppliers, menu items, and reports where both languages are operationally useful.

Prototype at least one real Arabic report during Foundations, not at the end of the project.

The prototype should include:

- Multiline Arabic
- Mixed Arabic and English
- Arabic names with Latin codes
- IQD numbers
- Negative values and parentheses
- Long tables
- Repeated table headers
- Page breaks
- Totals
- Signatures
- RTL column order
- Searchable and selectable text
- Printing from Windows
- Different PDF viewers

Do not assume a PDF library is safe for Arabic because a simple sentence looks correct. Verify shaping, bidirectional text, table layout, pagination, and text extraction.

Use a browser-rendered HTML/CSS approach as the primary candidate, such as Chromium through Playwright, and compare it against any alternative with the same test document. Keep the renderer behind a service interface so it can be replaced without changing report business logic.


Step 9 — Keep Claude within safe implementation boundaries

Claude should be permitted to:

- Inspect files
- Create and edit project files
- Run tests
- Run non-destructive formatters and linters
- Create migrations
- Start the development server
- Generate fixtures
- Produce reports in a development database

Claude should not independently:

- Delete production data
- Reset migrations
- Drop databases
- Force push
- Rewrite shared Git history
- Change production secrets
- Post financial opening balances
- Approve its own imported invoices
- Bypass failed tests
- Disable constraints to make tests pass

Use separate development and production credentials. The agent should not require production access to build the application.


PART 3 — AI FEATURES INSIDE THE PRODUCT

Add AI only after the core transactions, ledgers, posting rules, and reports are stable.

1. Supplier invoice ingestion

The user uploads a photograph or PDF.

The AI returns a structured draft:

- Supplier
- Invoice number
- Invoice date
- Item descriptions
- Quantities
- Units
- Unit prices
- Discounts
- Additional charges
- Total
- Suggested matches to the item master
- Confidence
- Warnings

A human reviews and confirms the draft. Only then does the normal Supplier Invoice service validate and post it.

The AI must not post directly to inventory or accounting. Keep:

Original file
AI extraction
Human edits
Final approved document
Posting result

This creates a complete audit trail and makes invoice ingestion a time saver rather than an uncontrolled accounting input.


2. Natural-language reporting

A manager may ask:

“كم كلفة المواد لشهر تموز مقارنة بحزيران؟”

The AI should translate the request into an approved report intent and parameters, not arbitrary raw SQL against the financial database.

Use:

- Whitelisted report definitions
- Parameter validation
- Branch authorization
- Date-range validation
- Read-only database access
- Approved reporting views
- Query limits
- Audit logs
- Clear display of metric definitions

The AI can explain results, but every number should come from a deterministic report query.


3. Variance and anomaly assistance

After enough trustworthy history exists, AI may help explain:

- Unusual food-cost increases
- Recipe usage variance
- Purchase-price spikes
- Stock-count discrepancies
- Application settlement gaps
- Overtime changes
- Supplier concentration

It should present hypotheses and supporting records, not silently change data or assert a cause without evidence.


THE ONE THING TO BE STRICT ABOUT

Do not let Claude build Phases 1–8 in parallel simply because it can generate code quickly.

The dependency chain is real:

- Recipe costing depends on inventory and purchase cost.
- Sales consumption depends on recipes.
- Application reconciliation depends on sales and accounting.
- Payroll payment depends on treasury and journal controls.
- Reports depend on stable definitions and ledgers.
- AI depends on trustworthy read models and human approval.

Complete the inventory core with passing tests, real opening data, reversals, counts, valuation, and reconciliation before allowing procurement to depend on it. Complete procurement costing before treating recipe cost as reliable. Complete recipes before using sales to calculate theoretical consumption. Complete the sales and settlement cycle before finalizing channel profitability and full financial reporting.

Speed should come from small, verified increments, not from generating a large unverified codebase.


DEFINITION OF DONE FOR EACH MODULE

A module is done only when all of the following are true:

- The SRS requirements are mapped.
- Unresolved business questions are recorded.
- The module specification is approved.
- Required ADRs exist.
- Models and database constraints exist.
- Migrations run forward on a clean database.
- Domain services implement the rules.
- Permissions and approvals are enforced.
- Posting and reversal are implemented.
- Idempotency is tested.
- Concurrency risks are tested.
- API or operational UI is usable.
- Admin support is safe.
- Audit records are complete.
- Imports have dry-run and reconciliation.
- Reports reconcile to the ledger.
- Unit, integration, and golden-case tests pass.
- Realistic branch sample data has been tested.
- The documentation explains how to operate and troubleshoot the module.
- No known critical or high-severity defect remains.


RECOMMENDED FIRST CLAUDE CODE PROMPT

You are acting as the principal software architect and senior Django engineer for Khan Mandi Restaurant Management System — Al-Bunook Branch, Baghdad, Iraq.

The attached SRS is the authoritative source for business rules. The repository may also contain architecture plans, previous code, spreadsheets, and recipe documents. Read all relevant project files before making a technical decision.

Your current task is Phase 0 and the preparation of Phase 1 Inventory. Do not build later modules.

First:

1. Inspect the repository structure, settings, dependencies, existing apps, migrations, tests, and documentation.
2. Read CLAUDE.md and follow it as mandatory project policy.
3. Read the complete SRS and create or update docs/requirements/traceability.md.
4. Identify contradictions, missing rules, and existing implementation gaps.
5. Do not write implementation code until the Foundations and Inventory specifications are internally consistent.
6. Ask only questions that cannot be answered from the project files, one question at a time.
7. Record major irreversible decisions as ADRs.

The approved architectural rules are:

- Use a Django modular monolith with PostgreSQL.
- Use Django Ninja and Pydantic for the API unless an existing approved project decision says otherwise.
- Use a service layer for commands and business transactions.
- Use selector/query services for complex reads and reports.
- Posted stock and accounting effects are append-only.
- Posted transactions are corrected by reversal, never direct editing or deletion.
- Stock on hand is derived from StockMovement.
- Account balances are derived from JournalEntry and JournalLine.
- Projections and snapshots must be rebuildable from the ledgers.
- Use Decimal for money, quantities, rates, unit cost, yield, and payroll calculations.
- Use moving weighted-average inventory costing according to a written policy.
- Negative stock is prohibited unless the SRS explicitly approves a controlled exception.
- Every stock or money operation requires tests.
- Critical posting logic must not be hidden in Django signals.
- Business documents and ledger entries are separate concepts.
- The procurement lifecycle separates purchase order, goods receipt, supplier invoice, supplier return, and payment.
- Unit conversion, packaging conversion, and production yield are separate concepts.
- Recipe versions use effective dates.
- Theoretical consumption and actual consumption are separate.
- Organization, branch, warehouse, kitchen location, and cash point are explicitly modeled.
- Store both timestamp and restaurant business date.
- Every posting operation is atomic and idempotent.
- Closed periods reject postings.
- Branch authorization is enforced in services and queries.
- Arabic and RTL behavior must be tested early.
- Do not reset migrations, delete data, disable constraints, or perform destructive Git operations without explicit approval.

For Inventory, investigate and specify:

- Item master and categories
- Base units and item-specific conversions
- Warehouses and kitchen locations
- Opening quantities and values
- Goods receipts as stock events
- Issues
- Kitchen issues and returns
- Warehouse and inter-branch transfers
- Supplier returns
- Waste
- Damage and expiry
- Staff meals and complimentary meals
- Stock counts
- Adjustments
- Reversals
- Moving weighted-average valuation
- Negative stock
- Backdated transactions
- Period locks
- Reorder levels
- Concurrency
- Idempotency
- Audit
- Branch permissions
- Reconciliation reports

Produce, in order:

1. A repository assessment.
2. A Foundations gap analysis.
3. docs/specs/foundations.md.
4. docs/specs/inventory.md.
5. Required ADRs.
6. Updated requirements traceability.
7. Inventory invariants.
8. Acceptance-test scenarios.
9. A proposed file and app structure.
10. A small implementation plan divided into reviewable commits.

After the specifications are complete, implement only the first approved vertical slice. For every change:

- Show the files to be changed.
- Write or update the test first.
- Run the focused test.
- Implement the behavior.
- Run the focused and relevant regression tests.
- Review the migration.
- Summarize the business rule implemented.
- Stop at a clean commit boundary before starting another concern.

Do not generate the entire ERP in one pass. Correct business logic, auditability, and reconciliation are more important than code volume.


FINAL ARCHITECTURAL POSITION

The system should be designed around immutable posted effects, explicit units, effective-dated business rules, deterministic costing, controlled accounting postings, branch-aware authorization, and reconciliable read models.

The most important retained principles are:

- Two append-only ledgers as the operational and financial spine
- Separate business documents from posted effects
- Unit conversion from day one
- Decimal and a single rounding policy
- Moving weighted average with a complete edge-case policy
- Recipe versions, yield, and theoretical-versus-actual consumption
- Sales and application settlement as a core module
- Thin accounting kernel early and full accounting after real workflows exist
- Business-date handling for after-midnight operations
- Tests before implementation for money and stock
- Small reviewed commits
- Human approval for AI-extracted financial data
- No uncontrolled raw SQL or direct AI posting
- No parallel construction of dependent modules

Following this order will produce a system that can explain every stock quantity, every unit cost, every application receivable, every supplier balance, every salary payment, and every journal balance back to its source transaction. That traceability is the difference between a restaurant CRUD application and a production-grade restaurant ERP.
