"""
The admin is not a way around the accounting kernel.

An editable admin form would be a second write path into the ledger — one
that skips the validators, takes no idempotency key, resolves no scope, and
writes no audit event. Tested through the registry and through real HTTP,
because `has_change_permission` returning False is only a claim until the
URL it guards actually refuses.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from apps.accounting.models import (
    Account,
    AccountingPeriod,
    AccountingSettings,
    CostCenter,
    FiscalYear,
    JournalEntry,
    JournalLine,
)
from apps.accounting.validators import PostingLine
from apps.core.models import AuditEvent
from apps.organizations.models import Branch, Organization, OrganizationMembership
from apps.users.models import User

from .conftest import POSTING_DATE

pytestmark = pytest.mark.django_db

LEDGER_MODELS = [
    JournalEntry,
    JournalLine,
    Account,
    CostCenter,
    AccountingPeriod,
    FiscalYear,
    AccountingSettings,
]


@pytest.fixture
def staff_admin() -> User:
    """A normal administrative user: staff, not superuser."""
    user = User.objects.create_user(
        username="ops-admin", password="pw-not-real-1234", is_staff=True
    )
    return User.objects.get(pk=user.pk)


class TestEveryAccountingModelIsRegisteredReadOnly:
    @pytest.mark.parametrize("model", LEDGER_MODELS)
    def test_it_is_registered(self, model: type) -> None:
        """Visible: an inspection tool that hides the ledger is not one."""
        assert model in admin.site._registry

    @pytest.mark.parametrize("model", LEDGER_MODELS)
    def test_it_refuses_add_change_and_delete(self, model: type, superuser: User, rf: Any) -> None:
        """
        Asked as a superuser on purpose. Read-only here is not about privilege
        level — there is no privilege that makes writing around the kernel
        correct.
        """
        model_admin = admin.site._registry[model]
        request = rf.get("/admin/")
        request.user = superuser

        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)

    @pytest.mark.parametrize("model", LEDGER_MODELS)
    def test_it_offers_no_bulk_actions(self, model: type) -> None:
        """A bulk action is a write path with a dropdown in front of it."""
        assert admin.site._registry[model].actions is None

    def test_the_journal_line_inline_is_read_only_too(self, superuser: User, rf: Any) -> None:
        """
        The inline is the one that would otherwise be editable: Django's
        default inline is a form, and it sits inside a page the entry admin
        already declares read-only.
        """
        entry_admin = admin.site._registry[JournalEntry]
        request = rf.get("/admin/")
        request.user = superuser

        (inline,) = entry_admin.inlines
        instance = inline(JournalEntry, admin.site)

        assert not instance.has_add_permission(request, None)
        assert not instance.has_change_permission(request)
        assert not instance.has_delete_permission(request)

    def test_every_journal_entry_field_is_read_only(self, superuser: User, rf: Any) -> None:
        model_admin = admin.site._registry[JournalEntry]
        request = rf.get("/admin/")
        request.user = superuser

        readonly = set(model_admin.get_readonly_fields(request))
        assert {"narration", "source_event", "status", "accounting_date"} <= readonly


class TestTheAdminUrlsRefuse:
    """The claim, checked against the URLs it is a claim about."""

    def _superuser_client(self, superuser: User) -> Client:
        client = Client()
        client.force_login(superuser)
        return client

    def test_the_add_page_is_refused(self, superuser: User) -> None:
        client = self._superuser_client(superuser)
        response = client.get(reverse("admin:accounting_journalentry_add"))
        assert response.status_code == 403

    def test_the_change_page_renders_but_does_not_save(
        self,
        superuser: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from apps.accounting.services import post_entry

        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="admin-view",
            narration="original narration",
        )
        client = self._superuser_client(superuser)
        url = reverse("admin:accounting_journalentry_change", args=[entry.pk])

        assert client.get(url).status_code == 200

        # A POST to the same URL is what an attacker or a stale tab would send.
        client.post(url, data={"narration": "rewritten through the admin"})

        entry.refresh_from_db()
        assert entry.narration == "original narration"

    def test_the_delete_page_is_refused(
        self,
        superuser: User,
        organization: Organization,
        cash: Account,
        sales: Account,
        branch: Branch,
        hall: CostCenter,
    ) -> None:
        from apps.accounting.services import post_entry

        entry = post_entry(
            organization=organization,
            accounting_date=POSTING_DATE,
            lines=[
                PostingLine(account=cash, branch=branch, debit=Decimal("100000")),
                PostingLine(
                    account=sales, branch=branch, credit=Decimal("100000"), cost_center=hall
                ),
            ],
            idempotency_key="admin-delete",
        )
        client = self._superuser_client(superuser)

        response = client.post(
            reverse("admin:accounting_journalentry_delete", args=[entry.pk]),
            data={"post": "yes"},
        )

        assert response.status_code == 403
        assert JournalEntry.objects.filter(pk=entry.pk).exists()

    def test_a_staff_user_without_view_permission_sees_nothing(self, staff_admin: User) -> None:
        client = Client()
        client.force_login(staff_admin)

        response = client.get(reverse("admin:accounting_journalentry_changelist"))

        assert response.status_code == 403


class TestOtherWriteSurfaces:
    def test_the_audit_trail_stays_read_only(self, superuser: User, rf: Any) -> None:
        """Task 0.5's guarantee, re-checked because Task 0.7 touched the admin."""
        model_admin = admin.site._registry[AuditEvent]
        request = rf.get("/admin/")
        request.user = superuser

        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)

    def test_organization_membership_is_read_only_in_the_admin(
        self, superuser: User, rf: Any
    ) -> None:
        """
        Granted through `grant_organization_access`, which also syncs the role
        groups that carry the permissions. A row made directly here would look
        like authority and grant none of it.
        """
        model_admin = admin.site._registry[OrganizationMembership]
        request = rf.get("/admin/")
        request.user = superuser

        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)
