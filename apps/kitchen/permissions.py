"""
The kitchen permissions, their scope, and which role holds them.

Identical machinery to `apps/inventory/permissions.py`,
`apps/accounting/permissions.py` and `apps/procurement/permissions.py`,
deliberately: a permission says *what*, a membership says *where*, and neither
alone is authorization (ADR-016).

Task 3.1 declared **three**; Task 3.2A adds the **five** the lifecycle needs,
and no more. The production, meal, report and import permissions still arrive
with the documents they guard, one task at a time, because a permission for a
workflow that does not exist is a grant nobody can audit.

The five are separate because the acts are separate, and the control only works
if four different people can hold them:

* `submit_recipe_version` — the preparer says the draft is ready to be read.
* `review_recipe_version` — one party signs one column. The storekeeper checks
  units and quantities, the accountant checks the costing evidence, the kitchen
  checks the method. None of them gains the right to *change* the recipe by
  reviewing it, which is why this is not folded into `manage_recipe`.
* `approve_recipe_version` — the manager's signature, the third of the
  workbook's three parties.
* `reject_recipe_version` — refusing is its own authority. Recording a doubt
  and ending a version are different acts, and the first should not require the
  second.
* `activate_recipe_version` — deciding that an agreed recipe takes effect on a
  date. Separate from approval because agreeing a recipe is correct and
  deciding it governs Sunday's costing are two decisions, and the second one
  moves money.

**Maker-checker is enforced on the actor, never on the permission.** A branch
manager legitimately holds both `submit_recipe_version` and
`approve_recipe_version`; what they may not do is use both on the same version.
Encoding that as "only some role may approve" would be a different and weaker
control, and it would break the moment a branch had one manager.

The split that matters is between the **card** and the **cost**. A cook reads
the recipe card, the quantities and the method — that is the job. What the dish
costs is a separate question with a separate permission, exactly as inventory
separates `view_valuation` from `view_stock` and procurement separates
`view_supplier_cost` from `view_supplier`. Cost columns are **omitted, not
blanked**: a blanked column tells the reader a number exists and that they are
not trusted with it, which is a different statement from the one intended.

Task 3.1 shipped no cost column, and Task 3.3 built one: `view_recipe_cost`
now guards the cost card, the snapshots and every money key in the payloads.

**Task 3.4 adds the two production permissions**, and they are the first in
this module scoped to a **warehouse** rather than to the organization. That is
deliberate and it is why `PermissionScope.WAREHOUSE` was declared in Task 3.1:
a recipe is organization property — one dish, one menu — but a production batch
is custody of one store's stock, and inventory already scopes custody that way
(RCP-051). A storekeeper who runs one branch's kitchen store has no business
drafting production against another's.

`create_production_batch` covers drafting **and** editing a draft. They are not
split, because there is no useful post that may create a batch and may not
correct the quantities on it before it is posted — and a permission nobody can
hold alone is a permission that only complicates the map.

**Task 3.5 adds the two posting permissions**, and they are separate from
`create_production_batch` because the acts are separate. Drafting consumes
nothing and a wrong draft is discarded; posting moves stock and writes a
journal, and a wrong posting is corrected by a reversal that stays on the
record forever. A kitchen where the person who weighs the ingredients also
commits the stock movement is a kitchen with no second pair of eyes on the one
step that touches the ledger.

`reverse_production_batch` is **elevated** and sits with the manager rather
than the storekeeper, exactly as the goods-receipt reversal does: undoing a
posted economic event is a supervisory act, and the person who made the
posting should not be the only one who can make it disappear.

**Cost visibility stays with `view_recipe_cost` alone.** Reading a production
batch exposes no recipe cost, no unit cost, no standard cost and no snapshot
value, and a test reads the raw bytes to prove the keys are absent rather than
null.
"""

from __future__ import annotations

from enum import Enum

from apps.organizations.models import Role

APP_LABEL = "kitchen"


class PermissionScope(Enum):
    """
    Where a kitchen permission is exercised.

    Task 3.1 uses one value. `ORGANIZATION_MASTER_DATA` needs the caller to
    *reach* the organization — a branch membership is enough, because a branch
    manager legitimately maintains the shared recipe master, exactly as they
    maintain the shared item and supplier masters.

    The other values are declared because Task 3.5's production permissions
    will be **warehouse**-scoped: a batch moves stock, and inventory already
    scopes custody that way (RCP-051). Declaring the enum in full now keeps
    that later addition from looking like a redesign.
    """

    ORGANIZATION_MASTER_DATA = "ORGANIZATION_MASTER_DATA"
    ORGANIZATION_AUTHORITY = "ORGANIZATION_AUTHORITY"
    BRANCH = "BRANCH"
    WAREHOUSE = "WAREHOUSE"


# --- The eight --------------------------------------------------------------

#: Arrives from Django's default set for the `Recipe` model rather than from
#: `Meta.permissions`, because declaring `view_recipe` again would be an
#: `auth.E005` clash with the builtin. The codename is the one this module
#: checks, so it is real — it simply comes from the other half of the table.
#: `apps.procurement` records the same arrangement for `view_supplier`.
VIEW_RECIPE = f"{APP_LABEL}.view_recipe"
MANAGE_RECIPE = f"{APP_LABEL}.manage_recipe"
VIEW_RECIPE_COST = f"{APP_LABEL}.view_recipe_cost"
SUBMIT_RECIPE_VERSION = f"{APP_LABEL}.submit_recipe_version"
REVIEW_RECIPE_VERSION = f"{APP_LABEL}.review_recipe_version"
APPROVE_RECIPE_VERSION = f"{APP_LABEL}.approve_recipe_version"
REJECT_RECIPE_VERSION = f"{APP_LABEL}.reject_recipe_version"
ACTIVATE_RECIPE_VERSION = f"{APP_LABEL}.activate_recipe_version"

#: Arrives from Django's default set for `ProductionBatch`, like `view_recipe`
#: does for `Recipe`: declaring it again in `Meta.permissions` would be an
#: `auth.E005` clash with the builtin.
VIEW_PRODUCTION = f"{APP_LABEL}.view_productionbatch"
CREATE_PRODUCTION_BATCH = f"{APP_LABEL}.create_production_batch"
POST_PRODUCTION_BATCH = f"{APP_LABEL}.post_production_batch"
REVERSE_PRODUCTION_BATCH = f"{APP_LABEL}.reverse_production_batch"

ALL_PERMISSIONS: tuple[str, ...] = (
    VIEW_RECIPE,
    MANAGE_RECIPE,
    VIEW_RECIPE_COST,
    SUBMIT_RECIPE_VERSION,
    REVIEW_RECIPE_VERSION,
    APPROVE_RECIPE_VERSION,
    REJECT_RECIPE_VERSION,
    ACTIVATE_RECIPE_VERSION,
    VIEW_PRODUCTION,
    CREATE_PRODUCTION_BATCH,
    POST_PRODUCTION_BATCH,
    REVERSE_PRODUCTION_BATCH,
)

PERMISSION_SCOPE: dict[str, PermissionScope] = {
    # A recipe is organization property. The dish is one dish; one branch must
    # not invent its own version of the group's menu (RCP-006). The lifecycle
    # permissions scope the same way for the same reason: approving a version
    # is a statement about the organization's menu, even when the version is
    # then activated for one branch.
    VIEW_RECIPE: PermissionScope.ORGANIZATION_MASTER_DATA,
    MANAGE_RECIPE: PermissionScope.ORGANIZATION_MASTER_DATA,
    VIEW_RECIPE_COST: PermissionScope.ORGANIZATION_MASTER_DATA,
    SUBMIT_RECIPE_VERSION: PermissionScope.ORGANIZATION_MASTER_DATA,
    REVIEW_RECIPE_VERSION: PermissionScope.ORGANIZATION_MASTER_DATA,
    APPROVE_RECIPE_VERSION: PermissionScope.ORGANIZATION_MASTER_DATA,
    REJECT_RECIPE_VERSION: PermissionScope.ORGANIZATION_MASTER_DATA,
    ACTIVATE_RECIPE_VERSION: PermissionScope.ORGANIZATION_MASTER_DATA,
    # The first **warehouse**-scoped kitchen permissions, and the reason the
    # enum has carried that value since Task 3.1. A batch is custody of one
    # store's stock, not a statement about the organization's menu.
    VIEW_PRODUCTION: PermissionScope.WAREHOUSE,
    CREATE_PRODUCTION_BATCH: PermissionScope.WAREHOUSE,
    POST_PRODUCTION_BATCH: PermissionScope.WAREHOUSE,
    REVERSE_PRODUCTION_BATCH: PermissionScope.WAREHOUSE,
}


# --- Which role holds what --------------------------------------------------

_FULL = frozenset(ALL_PERMISSIONS)

#: Runs a branch end to end, including its kitchen. The nearest post this
#: deployment has to the workbook's الشيف, which is also why no new Chef role
#: is invented here: `KM-RCP-004` assigns the approved quantity to chef **plus**
#: accountant **plus** manager, and inventing a role to hold one third of a
#: three-party control would misrepresent the control.
#:
#: Holds every lifecycle authority, and is still refused the moment it tries to
#: approve its own submission: the separation is between *people*, and the
#: service and the database both check the actor.
_MANAGER = frozenset(
    {
        VIEW_RECIPE,
        MANAGE_RECIPE,
        VIEW_RECIPE_COST,
        SUBMIT_RECIPE_VERSION,
        REVIEW_RECIPE_VERSION,
        APPROVE_RECIPE_VERSION,
        REJECT_RECIPE_VERSION,
        ACTIVATE_RECIPE_VERSION,
        VIEW_PRODUCTION,
        CREATE_PRODUCTION_BATCH,
        POST_PRODUCTION_BATCH,
        REVERSE_PRODUCTION_BATCH,
    }
)

#: Answers for the figures. Reads recipes and their costs and signs the costing
#: review; writes neither — inventing a dish is a kitchen act, not an
#: accounting one, and reviewing a recipe must not become a way to edit it.
_ACCOUNTING_MANAGER = frozenset(
    {VIEW_RECIPE, VIEW_RECIPE_COST, REVIEW_RECIPE_VERSION, VIEW_PRODUCTION}
)

#: The workbook assigns `كلفة الوحدة` and `كلفة المكون` to المحاسب, so the
#: accountant reads recipe cost by the kitchen's own arrangement — and signs
#: the costing-evidence review, which is the second of its three parties. No
#: `manage_recipe`: the accountant attests the evidence, never the quantities.
_ACCOUNTANT = frozenset({VIEW_RECIPE, VIEW_RECIPE_COST, REVIEW_RECIPE_VERSION, VIEW_PRODUCTION})

#: Issues ingredients against a recipe card, and has no business seeing what
#: they cost — the same boundary that keeps stock valuation away from the
#: person counting the shelves. Signs the quantity-and-unit review, which is
#: the one signature on `KM-RCP-004`'s page that is genuinely theirs, and gains
#: no approval authority by holding it.
#:
#: **Drafts production**, added by Task 3.4, **and posts it**, added by Task
#: 3.5. The storekeeper is the post that actually weighs what went into the pot,
#: so recording it is theirs, and so is committing it: the stock leaving the
#: store is the storekeeper's own custody and nobody else is standing at the
#: scale. They still read no cost, which is the whole point of keeping the two
#: permissions apart — a batch screen shows quantities, not money, and posting
#: one shows no valuation either.
#:
#: **No reversal.** Undoing a posted economic event is supervisory, and a post
#: that could both make and unmake its own movements is a post with no control
#: over it at all.
_STOREKEEPER = frozenset(
    {
        VIEW_RECIPE,
        REVIEW_RECIPE_VERSION,
        VIEW_PRODUCTION,
        CREATE_PRODUCTION_BATCH,
        POST_PRODUCTION_BATCH,
    }
)

#: Buys the ingredients a recipe names, so needs to read the card. Recipe cost
#: is a kitchen and accounting figure, not a purchasing one — and reading a
#: recipe confers no say in whether it is approved.
#:
#: **No production authority at all**, deliberately: what the kitchen cooked on
#: Tuesday is not a purchasing question, and reading it would be reading another
#: department's working papers.
_PURCHASING = frozenset({VIEW_RECIPE})

#: Reads what exists, never what it cost.
_VIEWER = frozenset({VIEW_RECIPE, VIEW_PRODUCTION})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.OWNER.value: _FULL,
    Role.ACCOUNTING_MANAGER.value: _ACCOUNTING_MANAGER,
    Role.MANAGER.value: _MANAGER,
    Role.STOREKEEPER.value: _STOREKEEPER,
    Role.PURCHASING.value: _PURCHASING,
    Role.ACCOUNTANT.value: _ACCOUNTANT,
    Role.VIEWER.value: _VIEWER,
    # A cashier handles takings, not recipes.
    Role.CASHIER.value: frozenset(),
}


def permissions_for_role(role: Role | str) -> frozenset[str]:
    """The kitchen permissions a role carries. Unknown roles carry none."""
    value = role.value if isinstance(role, Role) else str(role)
    return ROLE_PERMISSIONS.get(value, frozenset())


def scope_of(permission: str) -> PermissionScope:
    """Whether a permission is exercised over an organization, branch, or warehouse."""
    try:
        return PERMISSION_SCOPE[permission]
    except KeyError:  # pragma: no cover - a typo in a caller, not a state
        raise ValueError(f"{permission} is not a kitchen permission") from None


def sync_role_groups() -> None:
    """
    Write `ROLE_PERMISSIONS` into the role groups.

    Replaces each role's *kitchen* permissions and leaves every other app's
    alone, so this and the inventory, accounting and procurement equivalents
    can all run without trampling each other.
    """
    from django.contrib.auth.models import Permission

    from apps.organizations.permissions import group_for_role

    known: dict[str, Permission] = {
        f"{APP_LABEL}.{permission.codename}": permission
        for permission in Permission.objects.filter(content_type__app_label=APP_LABEL)
    }

    missing = [name for name in ALL_PERMISSIONS if name not in known]
    if missing:
        raise LookupError(f"kitchen permissions are not migrated: {sorted(missing)}")

    ours = set(known.values())
    for role, permission_names in ROLE_PERMISSIONS.items():
        group = group_for_role(role)
        wanted = {known[name] for name in permission_names}
        group.permissions.remove(*(ours - wanted))
        group.permissions.add(*wanted)
