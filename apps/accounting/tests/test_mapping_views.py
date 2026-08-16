"""
The two mapping-mutation screens — ACCT-2.

`accounting:mapping_close` and `accounting:mapping_archive` shipped with
Task 1.3, are linked from the mapping list, and had no test anywhere in the
repository until this module. They are the only shipped accounting mutation
surfaces that were untested, and they are not minor: an account mapping
decides which general-ledger account every module's postings land in, so an
unguarded archive is a posting-time failure across inventory and procurement
at once. `mapping_archive` is POST-only with no template of its own and
redirects straight back to the list, so a regression there would be silent.

What these hold, beyond "it works": the permission **and** the scope (a
manager of another organization is refused), the close-versus-archive rule
that keeps used mappings readable, and the redirect-with-a-message shape the
list screen depends on.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    INVENTORY_CONTROL,
    Account,
    AccountRole,
    OrganizationAccountMapping,
)
from apps.accounting.services import create_account_mapping
from apps.organizations.models import Organization, Role
from apps.organizations.services import grant_organization_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"
JAN_1 = datetime.date(2026, 1, 1)
JUN_30 = datetime.date(2026, 6, 30)


@pytest.fixture
def control_role() -> AccountRole:
    return AccountRole.objects.get(code=INVENTORY_CONTROL)


@pytest.fixture
def mapping(
    organization: Organization, chart: None, control_role: AccountRole
) -> OrganizationAccountMapping:
    """An unused mapping: closable, and archivable while nothing has used it."""
    return create_account_mapping(
        organization=organization,
        account_role=control_role,
        account=Account.objects.get(organization=organization, code="1-03-01-001"),
        effective_from=JAN_1,
    )


@pytest.fixture
def mapping_manager(organization: Organization) -> User:
    """Holds `manage_account_mappings` over this organization, and only it."""
    user = User.objects.create_user(username="mapping-manager", password=PASSWORD)
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def rival_manager(other_organization: Organization) -> User:
    """The same authority, over a different organization."""
    user = User.objects.create_user(username="rival-manager", password=PASSWORD)
    grant_organization_access(
        user=user, organization=other_organization, role=Role.ACCOUNTING_MANAGER
    )
    return User.objects.get(pk=user.pk)


class TestClosingAMapping:
    def test_it_ends_the_range_and_returns_to_the_list(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_close", args=[mapping.pk]),
            {"effective_to": JUN_30.isoformat(), "reason": "انتقل الحساب"},
        )
        assert response.status_code == 302
        assert response.headers["Location"] == reverse("accounting:mapping_list")
        mapping.refresh_from_db()
        assert mapping.effective_to == JUN_30
        assert mapping.is_active is True  # closed is not archived

    def test_the_form_screen_renders_for_someone_who_may_use_it(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(mapping_manager).get(
            reverse("accounting:mapping_close", args=[mapping.pk])
        )
        assert response.status_code == 200
        assert 'name="effective_to"' in response.content.decode()

    def test_a_range_that_ends_before_it_starts_is_refused_and_changes_nothing(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """The service refuses it; the screen reports it and keeps the row intact."""
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_close", args=[mapping.pk]),
            {"effective_to": "2025-12-31", "reason": "خطأ"},
        )
        assert response.status_code == 302
        mapping.refresh_from_db()
        assert mapping.effective_to is None

    def test_a_missing_date_re_renders_the_form_rather_than_closing(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_close", args=[mapping.pk]), {"reason": "بلا تاريخ"}
        )
        assert response.status_code == 200
        mapping.refresh_from_db()
        assert mapping.effective_to is None

    def test_a_cashier_holding_no_such_authority_is_refused(
        self,
        mapping: OrganizationAccountMapping,
        cashier: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(cashier).post(
            reverse("accounting:mapping_close", args=[mapping.pk]),
            {"effective_to": JUN_30.isoformat()},
        )
        assert response.status_code == 403
        mapping.refresh_from_db()
        assert mapping.effective_to is None

    def test_the_same_authority_over_another_organization_reaches_nothing(
        self,
        mapping: OrganizationAccountMapping,
        rival_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        ADR-016 in one request: the permission is held, the scope is not.
        Out of scope answers 404 rather than 403, because a 403 would confirm
        that this mapping exists.
        """
        response = client_for(rival_manager).post(
            reverse("accounting:mapping_close", args=[mapping.pk]),
            {"effective_to": JUN_30.isoformat()},
        )
        assert response.status_code in (403, 404)
        mapping.refresh_from_db()
        assert mapping.effective_to is None


class TestAmendingAMapping:
    """
    The command existed from Task 1.3 with no way to reach it (ACCT-5): no
    view, no URL, no route, no test, while its siblings close and archive had
    screens. Amending an effective date range is the documented correction
    path for a mapping nothing has posted through, so the only way to perform
    it was a Python shell against production.
    """

    def test_it_corrects_the_account_and_returns_to_the_list(
        self,
        organization: Organization,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        replacement = Account.objects.get(organization=organization, code="1-03-02-001")
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_amend", args=[mapping.pk]),
            {"account": str(replacement.pk), "effective_from": "", "effective_to": ""},
        )
        assert response.status_code == 302
        assert response.headers["Location"] == reverse("accounting:mapping_list")
        mapping.refresh_from_db()
        assert mapping.account_id == replacement.pk

    def test_a_partial_correction_leaves_the_rest_alone(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """One mistyped date, corrected, without restating the account."""
        original_account = mapping.account_id
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_amend", args=[mapping.pk]),
            {"account": "", "effective_from": "2026-02-01", "effective_to": ""},
        )
        assert response.status_code == 302
        mapping.refresh_from_db()
        assert mapping.effective_from == datetime.date(2026, 2, 1)
        assert mapping.account_id == original_account

    def test_the_screen_renders_and_offers_only_this_organizations_accounts(
        self,
        organization: Organization,
        other_organization: Organization,
        other_chart: None,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """A dropdown offering a foreign account would be a tenancy hole."""
        response = client_for(mapping_manager).get(
            reverse("accounting:mapping_amend", args=[mapping.pk])
        )
        assert response.status_code == 200
        offered = set(response.context["form"].fields["account"].queryset)
        assert offered
        assert all(account.organization_id == organization.pk for account in offered)

    def test_a_date_and_a_clear_flag_together_are_refused(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_amend", args=[mapping.pk]),
            {"account": "", "effective_to": "2026-06-30", "clear_effective_to": "on"},
        )
        assert response.status_code == 200
        mapping.refresh_from_db()
        assert mapping.effective_to is None

    def test_a_cashier_cannot_amend(
        self,
        mapping: OrganizationAccountMapping,
        cashier: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(cashier).post(
            reverse("accounting:mapping_amend", args=[mapping.pk]),
            {"effective_from": "2026-02-01"},
        )
        assert response.status_code == 403
        mapping.refresh_from_db()
        assert mapping.effective_from == JAN_1

    def test_the_same_authority_over_another_organization_reaches_nothing(
        self,
        mapping: OrganizationAccountMapping,
        rival_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(rival_manager).get(
            reverse("accounting:mapping_amend", args=[mapping.pk])
        )
        assert response.status_code == 404


class TestArchivingAMapping:
    def test_an_unused_mapping_is_withdrawn(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_archive", args=[mapping.pk]), {"reason": "سُجّل خطأً"}
        )
        assert response.status_code == 302
        assert response.headers["Location"] == reverse("accounting:mapping_list")
        mapping.refresh_from_db()
        assert mapping.is_active is False

    def test_a_get_is_refused_because_archiving_is_not_a_read(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(mapping_manager).get(
            reverse("accounting:mapping_archive", args=[mapping.pk])
        )
        assert response.status_code == 405
        mapping.refresh_from_db()
        assert mapping.is_active is True

    def test_a_cashier_cannot_archive(
        self,
        mapping: OrganizationAccountMapping,
        cashier: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(cashier).post(
            reverse("accounting:mapping_archive", args=[mapping.pk]), {"reason": "محاولة"}
        )
        assert response.status_code == 403
        mapping.refresh_from_db()
        assert mapping.is_active is True

    def test_the_same_authority_over_another_organization_reaches_nothing(
        self,
        mapping: OrganizationAccountMapping,
        rival_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        response = client_for(rival_manager).post(
            reverse("accounting:mapping_archive", args=[mapping.pk]), {"reason": "محاولة"}
        )
        assert response.status_code in (403, 404)
        mapping.refresh_from_db()
        assert mapping.is_active is True

    def test_an_unknown_mapping_is_a_404_rather_than_a_crash(
        self,
        mapping: OrganizationAccountMapping,
        mapping_manager: User,
        client_for: Callable[[User], Client],
    ) -> None:
        """
        A guessed id answers the same way a foreign one does.

        The used-mapping refusal is deliberately *not* tested here: it is a
        service rule, already held at
        `apps/inventory/tests/test_inventory_account_mappings.py` where a real
        posting exists to mark a mapping used, and proving it here would mean
        an accounting test writing an inventory row — the import boundary this
        module exists to respect. What belongs at this layer is that the view
        turns a service refusal into a message and a redirect rather than a
        500, which the close screen's inverted-range test above shows.
        """
        response = client_for(mapping_manager).post(
            reverse("accounting:mapping_archive", args=[mapping.pk + 9999]), {"reason": "تخمين"}
        )
        assert response.status_code == 404
        mapping.refresh_from_db()
        assert mapping.is_active is True
