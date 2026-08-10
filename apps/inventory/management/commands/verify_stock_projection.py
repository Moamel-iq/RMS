"""
Shadow-rebuild the stock projection and report every field that disagrees.

    manage.py verify_stock_projection --organization DEMO-KHAN-MANDI
    manage.py verify_stock_projection --organization KM --warehouse MAIN

The wider sibling of `verify_stock_ledger`: the same replay, scoped, and
comparing the whole projection row rather than quantity and value alone —
average cost, control account, last movement and last posted sequence too.

## Verify only, and why there is no `--apply`

There is no repair mode, and adding one was considered and rejected for this
task rather than forgotten.

A safe repair needs all of: an organization maintenance lock, a guarantee that
nothing is posting concurrently, an explicit flag, a stated reason, an
identified actor, a backup warning, one transaction, audit evidence, and a
final verification before commit. Any one of those missing turns "repair" into
"overwrite the evidence of a defect with a plausible number", which is worse
than the drift — because after it runs, nobody can tell what happened.

Drift between the ledger and its projection is not wear. The balances are a
*projection* of immutable movements and cannot legitimately differ from them,
so a difference means something wrote a balance that no posting produced. The
useful response is to read the mismatch, not to erase it.

Exit code 1 when anything disagrees, so CI and cron notice without a person
reading the output. Exit code 2 for a selector that names nothing, because
"checked an organization that does not exist" must not look like "clean".
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import CommandParser

from apps.core.console import SeedCommand
from apps.inventory.models import InventoryItem, Warehouse
from apps.inventory.reconciliation import verify_organization
from apps.organizations.models import Branch, Organization


class Command(SeedCommand):
    help = (
        "Replay immutable stock movements into a shadow projection and report every "
        "field where StockBalance disagrees. Read-only; there is no repair mode."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization",
            dest="organization_code",
            default="",
            help="Organization code. Default: every organization.",
        )
        parser.add_argument(
            "--branch", dest="branch_code", default="", help="Branch code, within the organization."
        )
        parser.add_argument(
            "--warehouse",
            dest="warehouse_code",
            default="",
            help="Warehouse code, within the branch or organization.",
        )
        parser.add_argument(
            "--item", dest="item_code", default="", help="Item code, within the organization."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        code = str(options.get("organization_code") or "").strip().upper()
        organizations = Organization.objects.all().order_by("code")
        if code:
            organizations = organizations.filter(code=code)
            if not organizations.exists():
                self.write(f"No organization with code {code}.")
                raise SystemExit(2)

        branch_code = str(options.get("branch_code") or "").strip().upper()
        warehouse_code = str(options.get("warehouse_code") or "").strip().upper()
        item_code = str(options.get("item_code") or "").strip().upper()

        # A narrowing selector that names nothing is an error, not an empty
        # clean result. Silence here would read as "verified" to whoever ran
        # it, which is the one answer it must never give by accident.
        if (branch_code or warehouse_code or item_code) and not code:
            self.write("--branch, --warehouse and --item need --organization.")
            raise SystemExit(2)

        total = 0
        checked = 0
        for organization in organizations:
            scope = self._resolve_scope(organization, branch_code, warehouse_code, item_code)
            if scope is None:
                raise SystemExit(2)
            branch_id, warehouse_id, item_id = scope

            mismatches = verify_organization(
                organization,
                branch_id=branch_id,
                warehouse_id=warehouse_id,
                item_id=item_id,
            )
            checked += 1
            total += len(mismatches)
            label = organization.code + self._scope_label(branch_code, warehouse_code, item_code)
            if mismatches:
                self.write(f"{label}: {len(mismatches)} mismatch(es)")
                for mismatch in mismatches:
                    self.write(f"  ! {mismatch}")
            else:
                self.write(f"{label}: projection matches the replayed ledger.")

        if total:
            self.write("")
            self.write(
                f"{total} mismatch(es) across {checked} organization(s). The balances are a "
                "projection of immutable movements and cannot legitimately differ from them, "
                "so this is a defect rather than drift. There is no repair mode on purpose: "
                "investigate the cause before posting anything further."
            )
            raise SystemExit(1)

    # -- scope -------------------------------------------------------------

    def _resolve_scope(
        self, organization: Organization, branch_code: str, warehouse_code: str, item_code: str
    ) -> tuple[int | None, int | None, int | None] | None:
        """
        Turn codes into ids inside this organization, or report and stop.

        Resolved **within the organization** rather than globally: a warehouse
        code that exists somewhere else must not silently widen the check.
        """
        branch_id: int | None = None
        warehouse_id: int | None = None
        item_id: int | None = None

        if branch_code:
            branch = Branch.objects.filter(organization=organization, code=branch_code).first()
            if branch is None:
                self.write(f"{organization.code}: no branch {branch_code}.")
                return None
            branch_id = branch.pk

        if warehouse_code:
            warehouses = Warehouse.objects.filter(
                branch__organization=organization, code=warehouse_code
            )
            if branch_id is not None:
                warehouses = warehouses.filter(branch_id=branch_id)
            warehouse = warehouses.first()
            if warehouse is None:
                self.write(f"{organization.code}: no warehouse {warehouse_code}.")
                return None
            warehouse_id = warehouse.pk

        if item_code:
            item = InventoryItem.objects.filter(organization=organization, code=item_code).first()
            if item is None:
                self.write(f"{organization.code}: no item {item_code}.")
                return None
            item_id = item.pk

        return branch_id, warehouse_id, item_id

    def _scope_label(self, branch_code: str, warehouse_code: str, item_code: str) -> str:
        parts = [part for part in (branch_code, warehouse_code, item_code) if part]
        return f" [{'/'.join(parts)}]" if parts else ""
