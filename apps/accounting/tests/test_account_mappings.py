"""
Account roles and organization mappings (ADR-019; Task 1.3 §V 1–4, 10–13).

The vocabulary is system-owned and immutable; the mappings are effective-dated
organization decisions; resolution never guesses.
"""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.utils import ProgrammingError

from apps.accounting.models import (
    INVENTORY_CONTROL,
    INVENTORY_OPENING_EQUITY,
    SYSTEM_INVENTORY_ROLES,
    Account,
    AccountRole,
    OrganizationAccountMapping,
)
from apps.accounting.services import (
    amend_account_mapping,
    archive_account_mapping,
    close_account_mapping,
    create_account_mapping,
    mapping_is_used,
    resolve_default_account,
)
from apps.organizations.models import Organization, Role
from apps.organizations.services import grant_organization_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

JAN_1 = datetime.date(2026, 1, 1)
JUN_30 = datetime.date(2026, 6, 30)
JUL_1 = datetime.date(2026, 7, 1)


@pytest.fixture
def control_role() -> AccountRole:
    return AccountRole.objects.get(code=INVENTORY_CONTROL)


@pytest.fixture
def equity_role() -> AccountRole:
    return AccountRole.objects.get(code=INVENTORY_OPENING_EQUITY)


@pytest.fixture
def inventory_account(organization: Organization, chart: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-01-001")


@pytest.fixture
def transit_account(organization: Organization, chart: None) -> Account:
    return Account.objects.get(organization=organization, code="1-03-02-001")


class TestTheRoleVocabulary:
    def test_the_seven_inventory_roles_are_seeded_by_migration(self) -> None:
        codes = set(
            AccountRole.objects.filter(domain="INVENTORY", is_system=True).values_list(
                "code", flat=True
            )
        )
        assert codes == {code for code, _ar, _en, _scope in SYSTEM_INVENTORY_ROLES}

    def test_the_seed_agrees_with_the_model_constants(self) -> None:
        """The migration duplicates the literals; this is the pact that they match."""
        for code, name_ar, _name_en, mapping_scope in SYSTEM_INVENTORY_ROLES:
            role = AccountRole.objects.get(code=code)
            assert role.name_ar == name_ar
            assert role.mapping_scope == mapping_scope
            assert role.is_system

    def test_a_system_role_cannot_be_renamed_at_the_database(
        self, control_role: AccountRole
    ) -> None:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                AccountRole.objects.filter(pk=control_role.pk).update(code="RENAMED")

    def test_a_system_role_cannot_be_deleted_at_the_database(
        self, control_role: AccountRole
    ) -> None:
        with pytest.raises((IntegrityError, ProgrammingError)):
            with transaction.atomic():
                AccountRole.objects.filter(pk=control_role.pk).delete()

    def test_only_the_two_item_scoped_roles_are_overridable(self) -> None:
        """
        `INVENTORY_CONTROL` because it is the one role whose account carries
        standing stock value, and `INVENTORY_CONSUMPTION` (Task 1.4) because
        what a thing is consumed *as* is a property of the thing — packaging,
        cleaning materials, and ingredients belong in different expense
        accounts. Everything else takes an organization default only, and a
        role gaining item scope is a deliberate decision, not a default.
        """
        from apps.accounting.models import INVENTORY_CONSUMPTION

        item_scoped = set(
            AccountRole.objects.filter(mapping_scope="ITEM").values_list("code", flat=True)
        )
        assert item_scoped == {INVENTORY_CONTROL, INVENTORY_CONSUMPTION}


class TestOrganizationMappings:
    def test_the_account_must_belong_to_the_same_organization(
        self,
        organization: Organization,
        other_organization: Organization,
        other_cash: Account,
        control_role: AccountRole,
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_account_mapping(
                organization=organization,
                account_role=control_role,
                account=other_cash,
                effective_from=JAN_1,
            )
        assert caught.value.code == "account_organization_mismatch"

    def test_the_account_must_be_postable(
        self, organization: Organization, group_account: Account, control_role: AccountRole
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            create_account_mapping(
                organization=organization,
                account_role=control_role,
                account=group_account,
                effective_from=JAN_1,
            )
        assert caught.value.code == "account_not_postable"

    def test_the_account_must_be_active(
        self, organization: Organization, inventory_account: Account, control_role: AccountRole
    ) -> None:
        from apps.accounting.services import archive_account

        archive_account(account=inventory_account)
        with pytest.raises(ValidationError) as caught:
            create_account_mapping(
                organization=organization,
                account_role=control_role,
                account=inventory_account,
                effective_from=JAN_1,
            )
        assert caught.value.code == "account_inactive"

    def test_effective_ranges_cannot_overlap(
        self,
        organization: Organization,
        inventory_account: Account,
        transit_account: Account,
        control_role: AccountRole,
    ) -> None:
        create_account_mapping(
            organization=organization,
            account_role=control_role,
            account=inventory_account,
            effective_from=JAN_1,
        )
        with pytest.raises(ValidationError) as caught:
            create_account_mapping(
                organization=organization,
                account_role=control_role,
                account=transit_account,
                effective_from=JUL_1,
            )
        assert caught.value.code == "mapping_period_overlaps"

    def test_the_database_refuses_a_raw_overlapping_insert(
        self,
        organization: Organization,
        inventory_account: Account,
        transit_account: Account,
        control_role: AccountRole,
    ) -> None:
        """The EXCLUDE constraint holds when the service is bypassed."""
        create_account_mapping(
            organization=organization,
            account_role=control_role,
            account=inventory_account,
            effective_from=JAN_1,
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                OrganizationAccountMapping.objects.create(
                    organization=organization,
                    account_role=control_role,
                    account=transit_account,
                    effective_from=JUL_1,
                    version=99,
                )

    def test_versions_increment_and_ranges_can_abut(
        self,
        organization: Organization,
        inventory_account: Account,
        transit_account: Account,
        control_role: AccountRole,
    ) -> None:
        first = create_account_mapping(
            organization=organization,
            account_role=control_role,
            account=inventory_account,
            effective_from=JAN_1,
        )
        close_account_mapping(mapping=first, effective_to=JUN_30)
        second = create_account_mapping(
            organization=organization,
            account_role=control_role,
            account=transit_account,
            effective_from=JUL_1,
        )
        assert (first.version, second.version) == (1, 2)

    def test_resolution_is_exact_by_date_and_never_guesses(
        self,
        organization: Organization,
        inventory_account: Account,
        transit_account: Account,
        control_role: AccountRole,
    ) -> None:
        first = create_account_mapping(
            organization=organization,
            account_role=control_role,
            account=inventory_account,
            effective_from=JAN_1,
        )
        close_account_mapping(mapping=first, effective_to=JUN_30)
        create_account_mapping(
            organization=organization,
            account_role=control_role,
            account=transit_account,
            effective_from=JUL_1,
        )
        assert (
            resolve_default_account(
                organization=organization, account_role=control_role, on_date=JUN_30
            ).account
            == inventory_account
        )
        assert (
            resolve_default_account(
                organization=organization, account_role=control_role, on_date=JUL_1
            ).account
            == transit_account
        )
        with pytest.raises(ValidationError) as caught:
            resolve_default_account(
                organization=organization,
                account_role=control_role,
                on_date=datetime.date(2025, 12, 31),
            )
        assert caught.value.code == "account_role_unmapped"
        assert "INVENTORY_CONTROL" in str(caught.value)

    def test_an_unused_mapping_may_be_amended_or_archived(
        self,
        organization: Organization,
        inventory_account: Account,
        transit_account: Account,
        control_role: AccountRole,
    ) -> None:
        mapping = create_account_mapping(
            organization=organization,
            account_role=control_role,
            account=inventory_account,
            effective_from=JAN_1,
        )
        assert not mapping_is_used(mapping)
        amended = amend_account_mapping(mapping=mapping, account=transit_account)
        assert amended.account == transit_account
        archived = archive_account_mapping(mapping=amended, reason="recorded in error")
        assert archived.is_active is False


class TestMappingAuthorization:
    """§V 11–12: injection is a 404; missing permission is a 403."""

    @pytest.fixture
    def mapping_manager(self, organization: Organization) -> User:
        user = User.objects.create_user(username="mapping-manager", password="pw-not-real-1234")
        grant_organization_access(
            user=user, organization=organization, role=Role.ACCOUNTING_MANAGER
        )
        return User.objects.get(pk=user.pk)

    def test_a_foreign_organization_mapping_is_a_404(
        self,
        mapping_manager: User,
        other_organization: Organization,
        other_cash: Account,
        equity_role: AccountRole,
    ) -> None:
        from apps.accounting.commands import close_account_role_mapping

        foreign = create_account_mapping(
            organization=other_organization,
            account_role=equity_role,
            account=other_cash,
            effective_from=JAN_1,
        )
        from apps.organizations.authorization import OutOfScope

        with pytest.raises(OutOfScope):
            close_account_role_mapping(
                actor=mapping_manager, mapping_id=foreign.pk, effective_to=JUN_30
            )

    def test_missing_permission_is_a_403(
        self,
        accountant: User,
        organization: Organization,
        inventory_account: Account,
        control_role: AccountRole,
    ) -> None:
        """A branch ACCOUNTANT reaches the organization and holds no mapping
        authority: refused with 403, not told the mapping does not exist."""
        from apps.accounting.commands import map_account_role

        with pytest.raises(PermissionDenied):
            map_account_role(
                actor=accountant,
                organization_id=organization.pk,
                account_role_id=control_role.pk,
                account_id=inventory_account.pk,
                effective_from=JAN_1,
            )

    def test_organization_authority_grants_mapping_management(
        self,
        mapping_manager: User,
        organization: Organization,
        inventory_account: Account,
        control_role: AccountRole,
    ) -> None:
        from apps.accounting.commands import map_account_role

        mapping = map_account_role(
            actor=mapping_manager,
            organization_id=organization.pk,
            account_role_id=control_role.pk,
            account_id=inventory_account.pk,
            effective_from=JAN_1,
        )
        assert mapping.account == inventory_account
