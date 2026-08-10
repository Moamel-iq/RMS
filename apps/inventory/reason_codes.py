"""
Inventory reason codes: the organization's own vocabulary for why stock went.

Master data, and deliberately so. Spoilage, breakage, over-portioning, a
freezer failure, a delivery dropped in the yard — those are one restaurant
group's categories. Baking them into an enum would mean every organization
inherits Khan Mandi's list and no organization can retire an entry without a
migration.

## What cannot change, and why

The **code** and **what it applies to** are frozen at creation, by database
trigger. Everything else may be edited.

The reason is retrospective truth. A waste report groups a year of postings by
reason code; if `SPOIL` could be re-pointed at count variances, every one of
those postings would silently change what it says happened, and no reader
could tell. Renaming `SPOIL` from "تلف" to "تلف طبيعي" changes nothing about
what was recorded — it clarifies it — so that is allowed.

Archiving sets `is_active = False`. It never deletes, and the unique
constraint therefore keeps the code **reserved forever**: reissuing a retired
code to mean something new would put two meanings behind one identity in the
history, which is the same defect as repurposing it, arrived at more slowly.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.inventory.models import InventoryReasonCode, ReasonCodeApplication
from apps.organizations.models import Organization


def canonical_code(code: str) -> str:
    """
    Upper case, trimmed. One spelling per meaning.

    `spoil`, `Spoil` and ` SPOIL ` are one operator typing the same thing three
    ways. Storing them as three codes would split a year of waste analysis into
    three buckets that each look like a different problem.
    """
    canonical = code.strip().upper()
    if not canonical:
        raise ValidationError(_("A reason code needs a code."), code="reason_code_required")
    return canonical


@transaction.atomic
def create_reason_code(
    *,
    organization: Organization,
    code: str,
    name_ar: str,
    applies_to: str,
    name_en: str = "",
    requires_comment: bool = False,
    requires_evidence: bool = False,
) -> InventoryReasonCode:
    """Add a reason to the organization's list."""
    if applies_to not in ReasonCodeApplication.values:
        raise ValidationError(
            _("%(value)s is not something a reason code can apply to."),
            code="unknown_reason_application",
            params={"value": applies_to},
        )
    canonical = canonical_code(code)
    if InventoryReasonCode.objects.filter(organization=organization, code=canonical).exists():
        # Named separately from the constraint violation so the screen can say
        # "that code is taken, possibly by an archived reason" rather than
        # showing an integrity error.
        raise ValidationError(
            _("Reason code %(code)s already exists in this organization."),
            code="reason_code_taken",
            params={"code": canonical},
        )

    reason_code = InventoryReasonCode(
        organization=organization,
        code=canonical,
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        applies_to=applies_to,
        requires_comment=requires_comment,
        requires_evidence=requires_evidence,
    )
    reason_code.full_clean()
    reason_code.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=reason_code,
        new_state=snapshot(reason_code),
    )
    return reason_code


@transaction.atomic
def update_reason_code(
    *,
    reason_code: InventoryReasonCode,
    name_ar: str,
    name_en: str = "",
    requires_comment: bool | None = None,
    requires_evidence: bool | None = None,
    is_active: bool | None = None,
) -> InventoryReasonCode:
    """
    Rename a reason, change what it demands, or archive it.

    `code` and `applies_to` are absent from the signature on purpose — there is
    no argument to pass, so there is no call site to review. The trigger refuses
    them as well, for everything that does not come through here.
    """
    locked = InventoryReasonCode.objects.select_for_update().get(pk=reason_code.pk)
    before = snapshot(locked)

    locked.name_ar = name_ar.strip()
    locked.name_en = name_en.strip()
    if requires_comment is not None:
        locked.requires_comment = requires_comment
    if requires_evidence is not None:
        locked.requires_evidence = requires_evidence
    if is_active is not None:
        locked.is_active = is_active
    locked.full_clean()
    locked.save(
        update_fields=[
            "name_ar",
            "name_en",
            "requires_comment",
            "requires_evidence",
            "is_active",
            "updated_at",
        ]
    )
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=before,
        new_state=snapshot(locked),
    )
    return locked


def archive_reason_code(
    *, reason_code: InventoryReasonCode, reason: str = ""
) -> InventoryReasonCode:
    """
    Retire a reason without deleting it.

    Deletion is not offered at all. `PROTECT` on every line that references one
    would refuse it for a used code anyway, and offering an operation that
    works only until the first posting is worse than not offering it.
    """
    return update_reason_code(
        reason_code=reason_code,
        name_ar=reason_code.name_ar,
        name_en=reason_code.name_en,
        is_active=False,
    )


def selectable_reason_codes(
    *, organization: Organization, applies_to: str
) -> QuerySet[InventoryReasonCode]:
    """
    The reasons a new document may choose: this organization's, for this use,
    still active.

    Archived codes stay readable on the documents that already carry them —
    which is why the filter is here, on what may be *selected*, and not on the
    manager.
    """
    return InventoryReasonCode.objects.filter(
        organization=organization, applies_to=applies_to, is_active=True
    ).order_by("code")
