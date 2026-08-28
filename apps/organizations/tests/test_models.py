"""Organization hierarchy: shape, scoping, and the constraints the database enforces."""

from __future__ import annotations

from datetime import time

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from apps.organizations.models import Branch, BranchMembership, Organization, Role
from apps.organizations.services import create_branch, create_organization
from apps.users.models import User

pytestmark = pytest.mark.django_db

OPENING = time(9, 0)


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="KM", name="خان مندي")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name="البنوك",
        business_day_start_time=OPENING,
    )


class TestOrganization:
    def test_code_is_upper_cased(self) -> None:
        assert create_organization(code=" km ", name="خان").code == "KM"

    def test_names_are_stored_in_both_languages(self, organization: Organization) -> None:
        assert organization.name == "خان مندي"
        assert organization.name == "Khan Mandi"

    def test_code_is_globally_unique(self, organization: Organization) -> None:
        with pytest.raises((IntegrityError, ValidationError)):
            create_organization(code="KM", name="آخر")

    def test_lowercase_input_is_normalized_not_rejected(self) -> None:
        """Case is a typing habit, not a different code."""
        assert create_organization(code="km-1", name="اسم").code == "KM-1"

    @pytest.mark.parametrize("bad", ["-KM", "_KM", "K M", "KM!", "", "خان"])
    def test_malformed_codes_are_rejected(self, bad: str) -> None:
        """Cases that are still invalid after upper-casing."""
        with pytest.raises(ValidationError):
            create_organization(code=bad, name="اسم")

    def test_database_rejects_a_malformed_code(self, organization: Organization) -> None:
        """Services validate, but bulk operations and raw SQL do not."""
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE organizations_organization SET code = %s WHERE id = %s",
                    ["bad code", organization.id],
                )

    def test_database_rejects_an_empty_name(self, organization: Organization) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE organizations_organization SET name = %s WHERE id = %s",
                    ["", organization.id],
                )


class TestBranch:
    def test_belongs_to_one_organization(self, branch: Branch, organization: Organization) -> None:
        assert branch.organization == organization
        assert list(organization.branches.all()) == [branch]

    def test_code_is_unique_within_an_organization(
        self, branch: Branch, organization: Organization
    ) -> None:
        with pytest.raises((IntegrityError, ValidationError)):
            create_branch(
                organization=organization,
                code="BUNOOK",
                name="مكرر",
                business_day_start_time=OPENING,
            )

    def test_two_organizations_may_reuse_the_same_branch_code(self, branch: Branch) -> None:
        """
        Uniqueness is scoped, not global. Otherwise one tenant's naming would
        constrain another's.
        """
        other = create_organization(code="OTHER", name="آخر")
        twin = create_branch(
            organization=other,
            code="BUNOOK",
            name="البنوك",
            business_day_start_time=OPENING,
        )
        assert twin.code == branch.code
        assert twin.organization != branch.organization

    def test_defaults_to_the_project_timezone(self, branch: Branch) -> None:
        assert branch.timezone == "Asia/Baghdad"

    @pytest.mark.parametrize("bad", ["Mars/Olympus", "Asia/Bagdad", "", "UTC+3"])
    def test_unknown_timezones_are_rejected(self, organization: Organization, bad: str) -> None:
        """A wrong zone silently corrupts every business date on the branch."""
        with pytest.raises(ValidationError):
            create_branch(
                organization=organization,
                code="TZ",
                name="اسم",
                business_day_start_time=OPENING,
                timezone=bad,
            )

    def test_business_day_start_time_is_required(self, organization: Organization) -> None:
        """
        No default. The cutoff is an open business question (ADR-008) and a
        default would quietly become the answer.
        """
        with pytest.raises(ValidationError):
            Branch(
                organization=organization,
                code="NOCUT",
                name="اسم",
            ).full_clean()

    def test_organization_cannot_be_deleted_while_branches_exist(
        self, branch: Branch, organization: Organization
    ) -> None:
        """History must survive. Deactivate instead."""
        with pytest.raises(ProtectedError):
            organization.delete()


class TestBranchMembership:
    def test_one_role_per_user_per_branch(self, branch: Branch) -> None:
        user = User.objects.create_user(username="accountant", password="pw-not-real-1234")
        BranchMembership.objects.create(user=user, branch=branch, role=Role.ACCOUNTANT)
        with pytest.raises(IntegrityError):
            BranchMembership.objects.create(user=user, branch=branch, role=Role.CASHIER)

    def test_a_user_may_hold_several_branches(
        self, branch: Branch, organization: Organization
    ) -> None:
        """The reason access is a relationship and not a field on User."""
        second = create_branch(
            organization=organization,
            code="KARRADA",
            name="الكرادة",
            business_day_start_time=OPENING,
        )
        user = User.objects.create_user(username="roving", password="pw-not-real-1234")
        BranchMembership.objects.create(user=user, branch=branch, role=Role.ACCOUNTANT)
        BranchMembership.objects.create(user=user, branch=second, role=Role.ACCOUNTANT)
        assert user.branch_memberships.count() == 2

    def test_branch_cannot_be_deleted_while_memberships_exist(self, branch: Branch) -> None:
        user = User.objects.create_user(username="someone", password="pw-not-real-1234")
        BranchMembership.objects.create(user=user, branch=branch, role=Role.VIEWER)
        with pytest.raises(ProtectedError):
            branch.delete()
