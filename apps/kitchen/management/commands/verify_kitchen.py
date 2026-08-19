"""
The Phase 3 composite verifier. Read-only, and it composes rather than repeats.

`python manage.py verify_kitchen` answers one question: **does everything the
Kitchen module claims still agree with the ledgers underneath it?**

## Composition, not reimplementation

Seven verifiers already exist and each owns its own equations:

| Verifier | Owns |
|---|---|
| `apps.kitchen.reconciliation` | recipe and version structure, effective dating, immutability, provenance, the component graph |
| `apps.kitchen.cost_reconciliation` | cost cards, snapshot totals, serving allocations, plate cost, append-only evidence |
| `apps.kitchen.production_reconciliation` | Task 3.4 draft invariants |
| `apps.kitchen.production_posting_reconciliation` | posted input/output, allocations, value conservation, the output lot, per-account journal nets and the legitimate no-journal case, reversal |
| `apps.inventory.reconciliation` (projection) | replaying movements into a shadow projection |
| `apps.inventory.reconciliation` (stock ledger) | the ledger against `StockBalance` |
| `apps.inventory.reconciliation` (accounting) | openings, balances and the general ledger against each other |

This command calls each of them and adds only what Task 3.8 created: the
movement partition and its stock identity, document-link integrity, batch
actual consumption against its movement evidence, and the theoretical coverage
limitation. Re-deriving any of the seven here would produce a second opinion
that agrees until the day it does not.

## Three severities, and only one of them is a failure

```
ERROR                — a real disagreement. Exit code 1.
ADVISORY             — worth a human's attention. Exit code unchanged.
COVERAGE_LIMITATION  — something is knowably absent. Exit code unchanged.
```

The third class is why this command is usable as a Phase 3 gate at all. These
are **not errors**:

* `SALES_NOT_INCLUDED_PHASE_4` — approved sold quantities arrive in Phase 4.
* `MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED` — the staff-benefit expense
  reclassification needs an approved journal shape that does not exist yet.
* `FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE` — follows from the first.

A verifier that exited non-zero for a missing *module* would be reporting that
Phase 4 has not happened, which everybody already knows, and the command would
be permanently red and therefore permanently ignored.

## No repair mode

There is no `--fix`, no `--repair`, no `--rebuild` (RCP-050). A verifier that
could change the thing it verifies is a verifier nobody can trust, and the one
situation where a repair is tempting — the numbers disagree — is exactly the
situation where a human needs to see them disagree first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.kitchen.consumption import FlowFilters
from apps.kitchen.consumption_reconciliation import (
    ADVISORY,
    CONSUMPTION_ADVISORIES,
    COVERAGE_LIMITATION,
    ERROR,
    Finding,
    verify_batch_consumption,
    verify_document_links,
    verify_movement_partition,
    verify_theoretical_coverage,
)
from apps.kitchen.consumption_sources import (
    FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE,
    MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED,
    MealUsageFilters,
    sales_source_is_registered,
)
from apps.kitchen.productivity import ProductionFilters
from apps.organizations.models import Organization


@dataclass(frozen=True)
class Section:
    """One verifier's contribution, with what it looked at."""

    title: str
    checked: str
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [row for row in self.findings if row.severity == ERROR]


def _error(code: str, message: str) -> Finding:
    return Finding(severity=ERROR, code=code, message=message)


def _advisory(code: str, message: str) -> Finding:
    return Finding(severity=ADVISORY, code=code, message=message)


def _limitation(code: str, message: str) -> Finding:
    return Finding(severity=COVERAGE_LIMITATION, code=code, message=message)


class Command(SeedCommand):
    help = (
        "Verify the whole Kitchen module against the ledgers underneath it: recipes, "
        "versions, costing, production drafts, posted production, productivity, meals, "
        "the movement partition, document links, consumption, and the Inventory and "
        "general-ledger verifiers. Read-only; there is no repair mode. Exits non-zero "
        "only for ERROR — a coverage limitation is not a defect."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            dest="organization_code",
            default="",
            help="Organization code. Default: every organization.",
        )
        parser.add_argument(
            "--user",
            dest="username",
            default="",
            help=(
                "Username whose scope the consumption reads run under. Those reads are "
                "scoped by warehouse membership, so without one they see nothing and "
                "the partition check would pass by reading no movements."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = str(options.get("organization_code") or "").strip().upper()
        organizations = Organization.objects.all().order_by("code")
        if code:
            organizations = organizations.filter(code=code)
            if not organizations.exists():
                self.write(f"No organization with code {code}.")
                raise SystemExit(2)

        actor = self._resolve_actor(str(options.get("username") or "").strip())

        total_errors = 0
        total_advisories = 0
        total_limitations = 0

        for organization in organizations:
            self.write("")
            self.write("=" * 72)
            self.write(f"{organization.code} — {organization.name_ar}")
            self.write("=" * 72)

            for section in self._organization_sections(organization):
                errors, advisories, limitations = self._render(section)
                total_errors += errors
                total_advisories += advisories
                total_limitations += limitations

        # The consumption reads are scoped by the **caller's** warehouse
        # memberships rather than by organization, so they run once. Running
        # them inside the loop above reported the same 76 movements twice and
        # doubled every advisory, which is how a summary count stops meaning
        # anything.
        self.write("")
        self.write("=" * 72)
        self.write("Caller-scoped checks (warehouse membership, not organization)")
        self.write("=" * 72)
        for section in self._caller_sections(actor):
            errors, advisories, limitations = self._render(section)
            total_errors += errors
            total_advisories += advisories
            total_limitations += limitations

        self._epilogue(total_errors, total_advisories, total_limitations)

    def _render(self, section: Section) -> tuple[int, int, int]:
        """Print one section and return its (error, advisory, limitation) counts."""
        self.write("")
        self.write(f"{section.title}")
        self.write(f"  checked: {section.checked}")
        if not section.findings:
            self.write("  clean")
        for finding in section.findings:
            self.write(f"  [{finding.severity:<19}] {finding.code}")
            self.write(f"                        {finding.message}")
        return (
            len([r for r in section.findings if r.severity == ERROR]),
            len([r for r in section.findings if r.severity == ADVISORY]),
            len([r for r in section.findings if r.severity == COVERAGE_LIMITATION]),
        )

    # -- the sections ------------------------------------------------------

    def _organization_sections(self, organization: Organization) -> list[Section]:
        """Checks whose subject is one organization's own rows."""
        return [
            self._recipes(organization),
            self._costing(organization),
            self._drafts(organization),
            self._postings(organization),
            self._meals(organization),
            self._links(organization),
            self._inventory(organization),
        ]

    def _caller_sections(self, actor: Any) -> list[Section]:
        """
        Checks scoped by the caller's warehouse memberships.

        These read across every organization the caller reaches, so repeating
        them per organization would count the same movements once per
        organization and inflate every total.
        """
        return [
            self._partition(actor),
            self._consumption(actor),
            self._coverage(actor),
        ]

    def _recipes(self, organization: Organization) -> Section:
        """Recipe and version structure, dating, immutability, the graph."""
        from apps.kitchen.reconciliation import (
            component_advisories,
            recipes_checked,
            verify_organization,
        )

        findings = [
            _error(row.code, f"{row.recipe_code} v{row.version}: {row.message}")
            for row in verify_organization(organization)
        ]
        findings += [
            _advisory(row.code, f"{row.recipe_code} v{row.version}: {row.message}")
            for row in component_advisories(organization)
        ]
        return Section(
            title="1. Recipes, versions, effective dating, immutability, component graph",
            checked=f"{recipes_checked(organization)} recipe(s)",
            findings=findings,
        )

    def _costing(self, organization: Organization) -> Section:
        """Cost cards, snapshot totals, serving allocations, append-only evidence."""
        from apps.kitchen.cost_reconciliation import snapshots_checked, verify_cost_snapshots

        findings = [
            _error(row.code, f"snapshot {row.snapshot_id} {row.recipe_code}: {row.message}")
            for row in verify_cost_snapshots(organization)
        ]
        return Section(
            title="2. Costing: cost cards, snapshot totals, serving allocations, plate cost",
            checked=f"{snapshots_checked(organization)} snapshot(s)",
            findings=findings,
        )

    def _drafts(self, organization: Organization) -> Section:
        """Task 3.4's draft invariants, unchanged and not re-derived."""
        from apps.kitchen.production_reconciliation import drafts_checked, verify_production_drafts

        findings = [
            (_error if row.is_blocking else _advisory)(
                row.code, f"batch {row.batch_id} {row.recipe_code}: {row.message}"
            )
            for row in verify_production_drafts(organization)
        ]
        return Section(
            title="3. Production drafts: Task 3.4 invariants",
            checked=f"{drafts_checked(organization)} draft(s)",
            findings=findings,
        )

    def _postings(self, organization: Organization) -> Section:
        """
        Posted production, including the legitimate no-journal case.

        `verify_production` recomputes each posted batch's per-account nets
        rather than reading `journal_entry`, because a journal that is rightly
        absent and one that is wrongly missing look identical from the outside
        (RCP-112 proof 5).
        """
        from apps.kitchen.production_posting_reconciliation import (
            posted_batches_checked,
            verify_production,
        )

        findings = [
            (_error if row.is_blocking else _advisory)(
                row.code, f"batch {row.batch_id} {row.recipe_code}: {row.message}"
            )
            for row in verify_production(organization)
        ]
        return Section(
            title=(
                "4. Posted production: input/output, allocations, value conservation, "
                "output lot, per-account journal nets or legitimate silence, reversal"
            ),
            checked=f"{posted_batches_checked(organization)} posting(s)",
            findings=findings,
        )

    def _meals(self, organization: Organization) -> Section:
        """
        Meals: the exact stored version, serving evidence, lifecycle, zero effect.

        The zero-stock and zero-GL claims are **measured** rather than trusted:
        `record_meal` has no path to either ledger, and this counts the rows
        that would exist if it did.
        """
        from apps.inventory.models import StockMovement
        from apps.kitchen.models import MealRecord, MealRecordStatus

        findings: list[Finding] = []
        records = MealRecord.objects.filter(organization=organization).select_related(
            "recipe_version", "serving", "recipe"
        )
        for record in records:
            if record.recipe_version.recipe_id != record.recipe_id:
                findings.append(
                    _error(
                        "kitchen_meal_version_belongs_to_another_recipe",
                        f"meal {record.public_id}: stored version is not this recipe's",
                    )
                )
            serving = record.serving
            if serving is not None and serving.version_id != record.recipe_version_id:
                findings.append(
                    _error(
                        "kitchen_meal_serving_version_mismatch",
                        f"meal {record.public_id}: serving belongs to another version",
                    )
                )
            if not serving and record.recipe_version.servings.exists():
                findings.append(
                    _error(
                        "kitchen_meal_serving_evidence_missing",
                        f"meal {record.public_id}: version defines servings but none is stored",
                    )
                )
            cancelled = record.status == MealRecordStatus.CANCELLED
            if cancelled and not (record.cancelled_at and record.cancellation_reason):
                findings.append(
                    _error(
                        "kitchen_meal_cancellation_evidence_incomplete",
                        f"meal {record.public_id}: cancelled without full evidence",
                    )
                )

        # A meal has no source document type in the ledger, so any movement
        # claiming one would be a movement nothing should have made.
        strays = StockMovement.objects.filter(
            organization=organization, entry__source_document_type="KITCHEN_MEAL_RECORD"
        ).count()
        if strays:
            findings.append(
                _error(
                    "kitchen_meal_moved_stock",
                    f"{strays} stock movement(s) claim a meal record as their source",
                )
            )
        findings.append(
            _limitation(
                MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED,
                "staff-meal cost is not reclassified to a staff-benefit expense account: "
                "the journal shape, the expense role and the theoretical-cost basis are "
                "not in any approved document (RCP-044). The records accumulate meanwhile.",
            )
        )
        return Section(
            title="5. Meals: exact version, serving evidence, lifecycle, zero stock, zero GL",
            checked=f"{records.count()} meal record(s)",
            findings=findings,
        )

    def _partition(self, actor: Any) -> Section:
        """
        Every movement classified exactly once, and the stock identity balancing.

        Skipped with an advisory rather than silently passing when no actor was
        given: the consumption reads are warehouse-scoped, so without a caller
        they read nothing, and a check that examined nothing would report clean.
        """
        if actor is None:
            return Section(
                title="6. Movement partition: one bucket per movement, stock identity",
                checked="nothing — pass --user",
                findings=[
                    _advisory(
                        "kitchen_partition_not_checked",
                        "the partition is warehouse-scoped and needs --user to read any "
                        "movement. Not checked, rather than checked and passed.",
                    )
                ],
            )
        findings = verify_movement_partition(actor, FlowFilters())
        from apps.kitchen.consumption import classified_movements

        return Section(
            title="6. Movement partition: one bucket per movement, stock identity",
            checked=f"{len(classified_movements(actor, FlowFilters()))} movement(s)",
            findings=findings,
        )

    def _links(self, organization: Organization) -> Section:
        """Attribution stays inside its source, and points at things that exist."""
        from apps.kitchen.models import BatchDocumentLink

        return Section(
            title=(
                "7. Document links: source exists and agrees, organization/warehouse/item "
                "match, attribution capped, no double attribution, no stock or GL effect"
            ),
            checked=f"{BatchDocumentLink.objects.filter(organization=organization).count()} link(s)",
            findings=verify_document_links(organization=organization),
        )

    def _consumption(self, actor: Any) -> Section:
        """Batch actual consumption against the movements the posting made."""
        if actor is None:
            return Section(
                title="8. Consumption: batch actuals agree with their movement evidence",
                checked="nothing — pass --user",
                findings=[
                    _advisory(
                        "kitchen_consumption_not_checked",
                        "batch consumption is warehouse-scoped and needs --user.",
                    )
                ],
            )
        findings = verify_batch_consumption(actor, ProductionFilters())
        return Section(
            title="8. Consumption: batch actuals agree with their movement evidence",
            checked="every posted batch in scope",
            findings=findings,
        )

    def _coverage(self, actor: Any) -> Section:
        """
        What the coverage report says about sales agrees with what is deployed.

        Both halves are computed from `sales_source_is_registered()` rather
        than asserted, because both answers are now facts about the
        deployment. Before Phase 4 the limitation was unconditional and right;
        continuing to print it once the adapter is registered would be a
        complete figure carrying a warning that it is incomplete — the exact
        failure `TheoreticalCoverage.notice` already refuses to make.

        Everything here is a `COVERAGE_LIMITATION` while the limitation holds,
        which does not affect the exit code: a missing module is not a defect in
        this one. The ERRORs `verify_theoretical_coverage` can still raise are
        disagreements between the coverage report and the registry, in either
        direction.
        """
        registered = sales_source_is_registered()
        title = (
            "9. Theoretical coverage: sales quantities included, finality claimed"
            if registered
            else "9. Theoretical coverage: sales limitation present, no finality claimed"
        )
        if actor is None:
            return Section(
                title=title,
                checked="nothing — pass --user",
                findings=(
                    []
                    if registered
                    else [
                        _limitation(
                            FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE,
                            "approved sales quantities do not exist before Phase 4, so no "
                            "final sales-based usage variance can be computed. Not "
                            "approximated.",
                        )
                    ]
                ),
            )
        findings = verify_theoretical_coverage(actor, MealUsageFilters())
        if not registered:
            findings.append(
                _limitation(
                    FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE,
                    "the variance screen shows a complete production standard variance and a "
                    "partial diagnostic labelled PARTIAL_COVERAGE / NOT_FINAL_USAGE_VARIANCE. "
                    "No surface offers a final figure.",
                )
            )
        return Section(
            title=title,
            checked="every declared theoretical source type",
            findings=findings,
        )

    def _inventory(self, organization: Organization) -> Section:
        """
        The three Inventory verifiers, run with production movements included.

        RCP-049 (4): `verify_inventory_against_gl` must stay clean **with**
        production movements in the ledger. It will by construction (RCP-037),
        and this is where construction meets reality.
        """
        from apps.inventory.reconciliation import verify_inventory_accounting, verify_organization

        findings = [
            _error(
                "inventory_projection_divergence",
                f"{row.warehouse_code}/{row.item_code}: {row.field} "
                f"projected {row.projected} != replayed {row.replayed}",
            )
            for row in verify_organization(organization)
        ]
        findings += [
            _error("inventory_gl_divergence", message)
            for message in verify_inventory_accounting(organization)
        ]
        return Section(
            title=(
                "10. Inventory: stock projection, stock ledger, inventory accounting, "
                "inventory against the general ledger"
            ),
            checked="every warehouse, item and lot in the organization",
            findings=findings,
        )

    # -- plumbing ----------------------------------------------------------

    def _resolve_actor(self, username: str) -> Any:
        from apps.users.models import User

        if not username:
            return None
        actor = User.objects.filter(username=username).first()
        if actor is None:
            self.write(f"No user named {username}.")
            raise SystemExit(2)
        return actor

    def _epilogue(self, errors: int, advisories: int, limitations: int) -> None:
        self.write("")
        self.write("=" * 72)
        self.write(f"ERROR:               {errors}")
        self.write(f"ADVISORY:            {advisories}")
        self.write(f"COVERAGE_LIMITATION: {limitations}")
        self.write("=" * 72)
        self.write("")
        self.write("The policies these checks exist to keep visible:")
        for advisory in CONSUMPTION_ADVISORIES:
            self.write(f"  - {advisory}")
        self.write("")
        # The sales half of this sentence is a fact about the deployment, so it
        # is answered rather than printed. A run whose theoretical figures now
        # include sold quantities must not close by telling the reader they do
        # not — the same reason `_coverage` stopped emitting the limitation.
        if sales_source_is_registered():
            self.write(
                "A COVERAGE_LIMITATION is not a defect. Sales quantities are included "
                "here; what remains is the meal expense reclassification, which needs "
                "an approved journal shape. That is not a disagreement between this "
                "module and a ledger."
            )
        else:
            self.write(
                "A COVERAGE_LIMITATION is not a defect. Sales quantities arrive in Phase 4 "
                "and the meal expense reclassification needs an approved journal shape; "
                "neither is a disagreement between this module and a ledger."
            )
        self.write("This command reports and refuses to repair. There is no --fix.")
        if errors:
            self.write("")
            self.write(f"{errors} ERROR finding(s). Exiting non-zero.")
            raise SystemExit(1)
        self.write("")
        self.write("No ERROR findings.")
