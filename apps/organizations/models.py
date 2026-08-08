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
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

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
    not enforced yet; this only records who holds which post at which branch.
    """

    OWNER = "OWNER", _("مالك")
    MANAGER = "MANAGER", _("مدير")
    ACCOUNTANT = "ACCOUNTANT", _("محاسب")
    PURCHASING = "PURCHASING", _("مسؤول مشتريات")
    STOREKEEPER = "STOREKEEPER", _("أمين مخزن")
    CASHIER = "CASHIER", _("أمين صندوق")
    VIEWER = "VIEWER", _("مطّلع")


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
    role = models.CharField(_("role"), max_length=20, choices=Role.choices)
    is_active = models.BooleanField(_("active"), default=True)

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

    def __str__(self) -> str:
        return f"{self.user} @ {self.branch.code} ({self.get_role_display()})"
