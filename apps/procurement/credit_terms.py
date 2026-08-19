"""Effective-dated supplier credit-term lifecycle and deterministic resolution."""

from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.context import get_actor
from apps.core.models import AuditAction
from apps.core.services import record_audit_event, snapshot
from apps.procurement.lifecycle import lock_and_require_status
from apps.procurement.models import Supplier, SupplierCreditTerm, SupplierCreditTermStatus
from apps.users.models import User

LEGACY_EFFECTIVE_FROM = datetime.date(1900, 1, 1)


def term_name_ar(net_days: int) -> str:
    """Stable Arabic label for system-created terms."""
    if net_days == 0:
        return "عند الاستلام"
    return f"{net_days} يوم"


def term_name_en(net_days: int) -> str:
    if net_days == 0:
        return "Due on receipt"
    return f"Net {net_days}"


def _validate_period(*, effective_from: datetime.date, effective_to: datetime.date | None) -> None:
    if effective_to is not None and effective_to < effective_from:
        raise ValidationError(
            _("The credit-term end date cannot precede its start date."),
            code="credit_term_period_invalid",
        )


def _overlaps(
    *, supplier: Supplier, effective_from: datetime.date, effective_to: datetime.date | None
) -> QuerySet[SupplierCreditTerm]:
    query = SupplierCreditTerm.objects.filter(
        supplier=supplier,
        status=SupplierCreditTermStatus.ACTIVE,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_from))
    if effective_to is not None:
        query = query.filter(effective_from__lte=effective_to)
    return query


@transaction.atomic
def bootstrap_credit_term(*, supplier: Supplier, net_days: int) -> SupplierCreditTerm:
    """
    Create the initial version used by existing supplier creation/import paths.

    This is the only system activation path. It exists so the legacy
    `Supplier.payment_terms_days` column becomes a projection immediately
    rather than remaining a competing source until a human creates a version.
    Later changes always use the draft/activation maker-checker workflow.
    """
    locked_supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
    existing = (
        SupplierCreditTerm.objects.filter(supplier=locked_supplier).order_by("version").first()
    )
    if existing is not None:
        return existing
    actor = get_actor()
    term = SupplierCreditTerm(
        organization=locked_supplier.organization,
        supplier=locked_supplier,
        version=1,
        status=SupplierCreditTermStatus.ACTIVE,
        name_ar=term_name_ar(net_days),
        name_en=term_name_en(net_days),
        net_days=net_days,
        effective_from=LEGACY_EFFECTIVE_FROM,
        created_by=actor,
    )
    term.full_clean()
    term.save()
    record_audit_event(
        action=AuditAction.CREATED,
        target=term,
        new_state=snapshot(term),
        metadata={"system_bootstrap": True},
    )
    return term


@transaction.atomic
def create_credit_term_draft(
    *,
    supplier: Supplier,
    name_ar: str,
    net_days: int,
    effective_from: datetime.date,
    created_by: User,
    name_en: str = "",
    effective_to: datetime.date | None = None,
    notes: str = "",
    supersedes: SupplierCreditTerm | None = None,
) -> SupplierCreditTerm:
    """Create the supplier's only editable credit-term version."""
    locked_supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
    _validate_period(effective_from=effective_from, effective_to=effective_to)
    if net_days < 0:
        raise ValidationError(_("Net days cannot be negative."), code="credit_term_days_negative")
    if not name_ar.strip():
        raise ValidationError(
            _("An Arabic credit-term name is required."), code="credit_term_name_required"
        )
    if SupplierCreditTerm.objects.filter(
        supplier=locked_supplier, status=SupplierCreditTermStatus.DRAFT
    ).exists():
        raise ValidationError(
            _("This supplier already has a draft credit term."),
            code="credit_term_draft_exists",
        )
    locked_supersedes: SupplierCreditTerm | None = None
    if supersedes is not None:
        locked_supersedes = SupplierCreditTerm.objects.select_for_update().get(pk=supersedes.pk)
        if locked_supersedes.supplier_id != locked_supplier.pk:
            raise ValidationError(
                _("The replaced term belongs to another supplier."),
                code="credit_term_supplier_mismatch",
            )
        if locked_supersedes.status != SupplierCreditTermStatus.ACTIVE:
            raise ValidationError(
                _("Only an active credit term can be replaced."),
                code="credit_term_supersedes_not_active",
            )
    version = (
        SupplierCreditTerm.objects.filter(supplier=locked_supplier).aggregate(value=Max("version"))[
            "value"
        ]
        or 0
    ) + 1
    term = SupplierCreditTerm(
        organization=locked_supplier.organization,
        supplier=locked_supplier,
        version=version,
        status=SupplierCreditTermStatus.DRAFT,
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        net_days=net_days,
        effective_from=effective_from,
        effective_to=effective_to,
        notes=notes.strip(),
        created_by=created_by,
        supersedes=locked_supersedes,
    )
    term.full_clean()
    term.save()
    record_audit_event(action=AuditAction.CREATED, target=term, new_state=snapshot(term))
    return term


@transaction.atomic
def update_credit_term_draft(
    *,
    term: SupplierCreditTerm,
    name_ar: str,
    name_en: str,
    net_days: int,
    effective_from: datetime.date,
    effective_to: datetime.date | None,
    notes: str,
) -> SupplierCreditTerm:
    locked = lock_and_require_status(
        SupplierCreditTerm,
        term.pk,
        {SupplierCreditTermStatus.DRAFT},
        code="credit_term_not_draft",
        message=_("Only a draft credit term can be edited."),
    )
    _validate_period(effective_from=effective_from, effective_to=effective_to)
    if net_days < 0:
        raise ValidationError(_("Net days cannot be negative."), code="credit_term_days_negative")
    if not name_ar.strip():
        raise ValidationError(
            _("An Arabic credit-term name is required."), code="credit_term_name_required"
        )
    previous = snapshot(locked)
    locked.name_ar = name_ar.strip()
    locked.name_en = name_en.strip()
    locked.net_days = net_days
    locked.effective_from = effective_from
    locked.effective_to = effective_to
    locked.notes = notes.strip()
    locked.full_clean()
    locked.save()
    record_audit_event(
        action=AuditAction.UPDATED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


@transaction.atomic
def delete_credit_term_draft(*, term: SupplierCreditTerm) -> None:
    locked = lock_and_require_status(
        SupplierCreditTerm,
        term.pk,
        {SupplierCreditTermStatus.DRAFT},
        code="credit_term_not_draft",
        message=_("Only a draft credit term can be deleted."),
    )
    previous = snapshot(locked)
    record_audit_event(action=AuditAction.DELETED, target=locked, previous_state=previous)
    locked.delete()


@transaction.atomic
def activate_credit_term(*, term: SupplierCreditTerm, actor: User) -> SupplierCreditTerm:
    """Activate one locked draft and, for a correction, close its predecessor."""
    locked = lock_and_require_status(
        SupplierCreditTerm,
        term.pk,
        {SupplierCreditTermStatus.DRAFT},
        code="credit_term_not_draft",
        message=_("Only a draft credit term can be activated."),
    )
    Supplier.objects.select_for_update().get(pk=locked.supplier_id)
    if locked.created_by_id == actor.pk:
        raise ValidationError(
            _("The creator cannot activate their own credit term."),
            code="credit_term_maker_checker",
        )
    previous = snapshot(locked)
    predecessor: SupplierCreditTerm | None = None
    if locked.supersedes_id is not None:
        predecessor = SupplierCreditTerm.objects.select_for_update().get(pk=locked.supersedes_id)
        if predecessor.status != SupplierCreditTermStatus.ACTIVE:
            raise ValidationError(
                _("The term being replaced is no longer active."),
                code="credit_term_supersedes_not_active",
            )
        if locked.effective_from <= predecessor.effective_from:
            raise ValidationError(
                _("A replacement must start after the version it replaces."),
                code="credit_term_replacement_date_invalid",
            )

    conflicts = _overlaps(
        supplier=locked.supplier,
        effective_from=locked.effective_from,
        effective_to=locked.effective_to,
    )
    if predecessor is not None:
        conflicts = conflicts.exclude(pk=predecessor.pk)
    if conflicts.select_for_update().exists():
        raise ValidationError(
            _("The active credit-term dates overlap another active version."),
            code="credit_term_overlap",
        )

    now = timezone.now()
    if predecessor is not None:
        predecessor_previous = snapshot(predecessor)
        predecessor.status = SupplierCreditTermStatus.SUPERSEDED
        predecessor.effective_to = locked.effective_from - datetime.timedelta(days=1)
        predecessor.save(update_fields=["status", "effective_to", "updated_at"])
        record_audit_event(
            action=AuditAction.UPDATED,
            target=predecessor,
            previous_state=predecessor_previous,
            new_state=snapshot(predecessor),
            reason=_("Superseded by credit-term version %(version)s") % {"version": locked.version},
        )

    locked.status = SupplierCreditTermStatus.ACTIVE
    locked.approved_by = actor
    locked.approved_at = now
    locked.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    Supplier.objects.filter(pk=locked.supplier_id).update(
        payment_terms_days=locked.net_days,
        updated_at=now,
    )
    record_audit_event(
        action=AuditAction.APPROVED,
        target=locked,
        previous_state=previous,
        new_state=snapshot(locked),
    )
    return locked


def resolve_credit_term(*, supplier: Supplier, on: datetime.date) -> SupplierCreditTerm | None:
    """The one activated version whose inclusive range covers ``on``.

    ``SUPERSEDED`` means that a newer version replaced the row; it does not
    erase the closed historical period in which that row was authoritative.
    This matters when an old supplier invoice is entered after the new terms
    were activated.
    """
    return (
        SupplierCreditTerm.objects.filter(
            supplier=supplier,
            status__in=(
                SupplierCreditTermStatus.ACTIVE,
                SupplierCreditTermStatus.SUPERSEDED,
            ),
            effective_from__lte=on,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on))
        .order_by("-effective_from", "-version")
        .first()
    )
