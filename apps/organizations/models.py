"""
Organization, Branch, and branch access.

See ADR-007 for the boundary rules and ADR-008 for the business-date fields.

Warehouse, kitchen location, cash point, and cost centre are deliberately
absent: they belong to the modules that own them, and modelling them here
would fix their shape before Inventory has requirements.
"""

from __future__ import annotations

import zoneinfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.core.models import TimeStampedModel

#: Codes are used in document numbering and reports, so they are constrained to
#: characters that survive filenames, URLs, and RTL rendering unambiguously.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_-]*$"


def validate_timezone(value: str) -> None:
    """Reject any zone the IANA database does not know.

    An unknown timezone would silently make every business date on the branch
    wrong, which is unrecoverable once transactions exist.
    """
    if value not in zoneinfo.available_timezones():
        raise ValidationError(
            _("%(value)s is not a known IANA timezone."),
            code="unknown_timezone",
            params={"value": value},
        )


class Organization(TimeStampedModel):
    """The top business boundary. Everything else hangs beneath one of these."""

    code = models.CharField(_("code"), max_length=20, unique=True)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200)
    is_active = models.BooleanField(_("active"), default=True)

    #: Mutable master data, so row history is kept. Posted ledger entries are
    #: NOT historied — they are immutable by construction and corrected by
    #: reversal, so a history table for them would only record tampering.
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN),
                name="organization_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en=""),
                name="organization_names_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class Branch(TimeStampedModel):
    """A trading location belonging to exactly one organization."""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="branches",
        verbose_name=_("organization"),
    )
    code = models.CharField(_("code"), max_length=20)
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200)

    timezone = models.CharField(
        _("timezone"),
        max_length=64,
        default=settings.TIME_ZONE,
        validators=[validate_timezone],
        help_text=_("IANA timezone in which this branch's operating day is measured."),
    )

    business_day_start_time = models.TimeField(
        _("business day start time"),
        help_text=_(
            "The operating day runs 24 hours from this local time. A sale after "
            "midnight belongs to the day that started the previous morning."
        ),
    )

    is_active = models.BooleanField(_("active"), default=True)

    #: Changing a branch's cutoff or timezone silently restates business dates,
    #: so the previous values must remain visible.
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("branch")
        verbose_name_plural = _("branches")
        ordering = ["organization__code", "code"]
        constraints = [
            # Scoped, not global: two organizations may each run a "BUNOOK".
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="branch_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN),
                name="branch_code_format",
            ),
            models.CheckConstraint(
                condition=~Q(name_ar="") & ~Q(name_en=""),
                name="branch_names_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name_ar}"


class Role(models.TextChoices):
    """
    Roles named in the separation-of-duties section of the architecture
    charter. NOT sourced from an SRS — no SRS exists. Approval thresholds are
    not enforced yet; this only records who holds which post.

    A role is held either at a branch (`BranchMembership`) or across a whole
    organization (`OrganizationMembership`). The role names what someone may
    do; the membership names where.
    """

    OWNER = "OWNER", _("مالك")
    ACCOUNTING_MANAGER = "ACCOUNTING_MANAGER", _("مدير الحسابات")
    MANAGER = "MANAGER", _("مدير")
    ACCOUNTANT = "ACCOUNTANT", _("محاسب")
    PURCHASING = "PURCHASING", _("مسؤول مشتريات")
    STOREKEEPER = "STOREKEEPER", _("أمين مخزن")
    CASHIER = "CASHIER", _("أمين صندوق")
    VIEWER = "VIEWER", _("مطّلع")


class WarehouseScopeMode(models.TextChoices):
    """How much of a branch's stock custody a membership reaches."""

    ALL = "ALL", _("كل المخازن")
    SELECTED = "SELECTED", _("مخازن محددة")


#: A custom role's code: lower-case, so it can never be mistaken for one of the
#: upper-case built-in values, and URL-safe, because it is part of a group name.
ROLE_CODE_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class RoleDefinition(TimeStampedModel):
    """
    A post the organization defined itself (ADR-034).

    The eight `Role` values are the charter's separation-of-duties posts. This
    is the owner's own: "an accountant I configure" — which screens that
    person sees and which acts they may perform, chosen permission by
    permission. It is granted exactly like a built-in role: a membership
    stores `key`, and `role:<key>` is the group carrying `permissions`.

    Defined per organization because its group carries permissions that this
    organization decided. Granting it inside another organization is refused
    by the grant services, not merely unusual.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="role_definitions",
        verbose_name=_("organization"),
    )
    code = models.CharField(
        _("code"),
        max_length=24,
        validators=[
            RegexValidator(
                ROLE_CODE_PATTERN,
                _("الرمز بحروف لاتينية صغيرة وأرقام وشرطات فقط، ويبدأ بحرف أو رقم."),
            )
        ],
    )
    name_ar = models.CharField(_("name (Arabic)"), max_length=200)
    name_en = models.CharField(_("name (English)"), max_length=200, blank=True)
    description = models.TextField(_("description"), blank=True)
    #: The built-in post this one was started from, if any. Kept for the
    #: record, never consulted for authority: the permissions are the truth.
    based_on = models.CharField(_("based on"), max_length=20, blank=True, choices=Role.choices)
    permissions = models.ManyToManyField(
        "auth.Permission",
        blank=True,
        related_name="role_definitions",
        verbose_name=_("permissions"),
    )
    is_active = models.BooleanField(_("active"), default=True)

    #: Who held authority to do what, and since when — the question an
    #: auditor asks when an act was performed under a post that has since
    #: changed shape.
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("role definition")
        verbose_name_plural = _("role definitions")
        ordering = ["organization__code", "name_ar"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="role_definition_code_unique_per_organization",
            ),
            models.CheckConstraint(
                condition=Q(code__regex=ROLE_CODE_PATTERN),
                name="role_definition_code_format",
            ),
        ]

    @property
    def key(self) -> str:
        """The value a membership stores, and the suffix of the group name."""
        return f"custom:{self.organization_id}:{self.code}"

    def __str__(self) -> str:
        return f"{self.organization.code} · {self.name_ar}"


class BranchMembership(TimeStampedModel):
    """
    Grants one user access to one branch in one role.

    Access is a relationship rather than a field on User, because an
    accountant may cover several branches. See ADR-007.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="branch_memberships",
        verbose_name=_("user"),
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="memberships",
        verbose_name=_("branch"),
    )
    #: A built-in `Role` value, or a custom role's key (`custom:<org>:<code>`).
    #: No `choices`: the valid set depends on the place the grant is made in,
    #: and the grant services validate it there (ADR-034).
    role = models.CharField(_("role"), max_length=64)
    is_active = models.BooleanField(_("active"), default=True)

    #: How far inside the branch this membership reaches. `ALL` covers every
    #: warehouse the branch has now and every one it opens later; `SELECTED`
    #: covers only the rows in `BranchMembershipWarehouse`.
    #:
    #: The default is `ALL` so that introducing warehouse scope cannot
    #: silently revoke anybody's access — restriction is opt-in, and that is
    #: the safe direction to get wrong.
    warehouse_scope_mode = models.CharField(
        _("warehouse scope"),
        max_length=10,
        choices=WarehouseScopeMode.choices,
        default=WarehouseScopeMode.ALL,
    )

    #: Who held which post, and when. The separation-of-duties question an
    #: auditor asks months later.
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("branch membership")
        verbose_name_plural = _("branch memberships")
        ordering = ["branch__code", "user__username"]
        constraints = [
            # One role per user per branch. Two rows would make "the" role
            # ambiguous at exactly the moment a permission check needs it.
            models.UniqueConstraint(
                fields=["user", "branch"],
                name="membership_unique_per_user_and_branch",
            ),
        ]

    @property
    def role_label(self) -> str:
        from apps.organizations.roles import role_label

        return role_label(self.role)

    def __str__(self) -> str:
        return f"{self.user} @ {self.branch.code} ({self.role_label})"


class BranchMembershipWarehouse(models.Model):
    """
    One warehouse a `SELECTED`-scope membership may reach.

    Deliberately not a membership of its own and deliberately carrying no
    role. A second role-bearing model would be a second place that grants
    authority, and eventually two answers to "what may this person do here".
    This only ever *narrows* what the branch membership already granted.

    Rows are ignored while the membership is in `ALL` mode — kept rather than
    deleted, so switching a membership to `SELECTED` and back does not lose
    the operator's choices.
    """

    branch_membership = models.ForeignKey(
        BranchMembership,
        on_delete=models.CASCADE,
        related_name="warehouse_scopes",
        verbose_name=_("branch membership"),
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.CASCADE,
        related_name="membership_scopes",
        verbose_name=_("warehouse"),
    )

    class Meta:
        verbose_name = _("branch membership warehouse")
        verbose_name_plural = _("branch membership warehouses")
        ordering = ["branch_membership_id", "warehouse__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch_membership", "warehouse"],
                name="branch_membership_warehouse_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.branch_membership} -> {self.warehouse.code}"


class OrganizationMembership(TimeStampedModel):
    """
    Grants one user authority across a whole organization, in one role.

    Not the same thing as holding the role at every branch. Some accounting
    state is organization-level and has no branch to be scoped to: a fiscal
    period spans every branch at once, so soft-closing, closing, or reopening
    one from a single branch's authority would let one location stop or
    reopen posting for all the others (ADR-013).

    Branch authority never adds up to organization authority. A user who
    happens to hold a role at all three branches today would silently lose
    organization scope the moment a fourth branch opened, which is exactly the
    kind of accidental permission change an audit cannot explain.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
        verbose_name=_("user"),
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="memberships",
        verbose_name=_("organization"),
    )
    #: A built-in `Role` value, or a custom role's key (`custom:<org>:<code>`).
    #: No `choices`: the valid set depends on the place the grant is made in,
    #: and the grant services validate it there (ADR-034).
    role = models.CharField(_("role"), max_length=64)
    is_active = models.BooleanField(_("active"), default=True)

    #: Who held organization-wide authority, and when. The first question an
    #: auditor asks about a reopened period.
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("organization membership")
        verbose_name_plural = _("organization memberships")
        ordering = ["organization__code", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="org_membership_unique_per_user_and_organization",
            ),
        ]

    @property
    def role_label(self) -> str:
        from apps.organizations.roles import role_label

        return role_label(self.role)

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization.code} ({self.role_label})"
