"""
Import batch constraints at a real COMMIT boundary.

Split out of `test_imports_and_projection.py` so that file can share one
demo seed across its module. A transactional test needs a genuine COMMIT
and truncates the tables afterwards, neither of which is possible inside
the outer block a shared seed holds open — the constraint would appear to
hold without ever having been tested against a committed row.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import transaction

from apps.inventory.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportKind,
)
from apps.organizations.services import create_organization
from apps.users.models import User


@pytest.fixture
def owner(units: None) -> User:
    return User.objects.create_user(username="import-owner", password="pw-not-real-1234")


@pytest.fixture
def seeded(owner: User, settings: object) -> None:
    settings.DEBUG = True  # type: ignore[attr-defined]
    call_command("seed_inventory_demo", user=owner.username, confirm_demo=True, stdout=StringIO())


@pytest.mark.django_db(transaction=True)
class TestImportConstraints:
    """
    The constraints that carry part of the correctness claim.

    Real COMMIT boundaries, because a check that only ever ran inside a
    rolled-back transaction has not been shown to hold when it matters.
    """

    def test_a_branch_scoped_kind_must_name_a_branch(self, settings: object) -> None:
        settings.DEBUG = True  # type: ignore[attr-defined]
        user = User.objects.create_user(username="c1", password="pw-not-real-1234")
        call_command("seed_units", verbosity=0)
        organization = create_organization(code="CONSTR", name="ق")
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError), transaction.atomic():
            ImportBatch.objects.create(
                organization=organization,
                branch=None,
                kind=ImportKind.BRANCH_ITEM_SETTING,
                original_filename="x.csv",
                content_hash="deadbeef",
                byte_size=10,
                row_count=0,
                valid_row_count=0,
                error_row_count=0,
                uploaded_by=user,
            )

    def test_the_row_counts_must_add_up(self) -> None:
        from django.db.utils import IntegrityError

        organization = create_organization(code="CONSTR2", name="ق")
        with pytest.raises(IntegrityError), transaction.atomic():
            ImportBatch.objects.create(
                organization=organization,
                kind=ImportKind.ITEM_CATEGORY,
                original_filename="x.csv",
                content_hash="deadbeef",
                byte_size=10,
                row_count=5,
                valid_row_count=1,
                error_row_count=1,
                uploaded_by=None,
            )

    def test_a_failed_batch_cannot_claim_to_have_applied_rows(self) -> None:
        from django.db.utils import IntegrityError

        organization = create_organization(code="CONSTR3", name="ق")
        with pytest.raises(IntegrityError), transaction.atomic():
            ImportBatch.objects.create(
                organization=organization,
                kind=ImportKind.ITEM_CATEGORY,
                status=ImportBatchStatus.FAILED_VALIDATION,
                original_filename="x.csv",
                content_hash="deadbeef",
                byte_size=10,
                row_count=2,
                valid_row_count=1,
                error_row_count=1,
                applied_row_count=1,
                uploaded_by=None,
            )
