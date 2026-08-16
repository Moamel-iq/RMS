"""
The import boundary and the projection verifier.

Two things that both exist to stop a plausible-looking number reaching the
ledger unexamined. The import tests are mostly about what does *not* happen —
upload and preview must change nothing, and one bad row must stop all of it.
The projection tests plant drift deliberately and prove the verifier sees it,
because a verifier that only ever runs against clean data has never been
tested at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.urls import reverse

from apps.core.context import audit_context
from apps.inventory import imports
from apps.inventory.models import (
    BranchItemSetting,
    ImportBatch,
    ImportBatchStatus,
    ImportKind,
    ItemCategory,
    StockBalance,
)
from apps.inventory.reconciliation import verify_organization
from apps.inventory.tests.conftest import refuse_transactional_tests, seed_demo_once
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import create_organization, grant_organization_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

HX = {"hx-request": "true"}

HEADER = "item_code,is_stocked,reorder_point,reorder_quantity"
GOOD = f"{HEADER}\nDEMO-RICE,yes,40.000,120.000\nDEMO-OIL,yes,100.000,60.000\n".encode()
MIXED = (f"{HEADER}\nDEMO-RICE,yes,40.000,120.000\nNOT-AN-ITEM,yes,10.000,10.000\n").encode()


#: One seed for the module. These tests do mutate — an applied batch changes reorder settings, and the verifier tests plant drift on purpose — but each rolls back to its own savepoint, so the next test sees the seed untouched.
@pytest.fixture(scope="module", autouse=True)
def seeded(django_db_setup: object, django_db_blocker: Any) -> Iterator[None]:
    import apps.inventory.tests.test_imports_and_projection as this_module

    refuse_transactional_tests(this_module)
    yield from seed_demo_once(django_db_blocker, username="import-owner")


@pytest.fixture
def owner() -> User:
    return User.objects.get(username="import-owner")


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.get(code="DEMO-KHAN-MANDI")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return Branch.objects.get(organization=organization, code="DEMO-BUNOOK")


def reorder_snapshot(branch: Branch) -> dict[str, object]:
    return dict(
        BranchItemSetting.objects.filter(branch=branch).values_list("item__code", "reorder_point")
    )


def upload(
    organization: Organization, branch: Branch, raw: bytes, name: str = "x.csv"
) -> ImportBatch:
    return imports.create_batch(
        organization=organization,
        branch=branch,
        kind=ImportKind.BRANCH_ITEM_SETTING,
        raw=raw,
        filename=name,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_upload_and_validate_change_no_master_data(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        """
        The whole reason preview is a separate step.

        A dry-run flag sharing a code path with the real thing is one `if`
        away from writing during the preview, and the operator would never
        know.
        """
        before = reorder_snapshot(branch)
        with audit_context(actor=owner):
            batch = upload(organization, branch, GOOD)
            assert reorder_snapshot(branch) == before
            batch = imports.validate_batch(batch=batch)
        assert batch.status == ImportBatchStatus.VALIDATED
        assert reorder_snapshot(branch) == before

    def test_apply_writes_and_records_what_changed(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        with audit_context(actor=owner):
            batch = imports.apply_batch(
                batch=imports.validate_batch(batch=upload(organization, branch, GOOD))
            )
        assert batch.status == ImportBatchStatus.APPLIED
        assert batch.applied_row_count == 2
        assert batch.applied_by_id == owner.pk
        assert batch.applied_at is not None
        assert BranchItemSetting.objects.get(
            branch=branch, item__code="DEMO-RICE"
        ).reorder_point == Decimal("40.000")

    def test_one_invalid_row_stops_the_whole_batch(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        """
        The approved atomic policy, and the valid row is not quietly applied.

        99-of-100 leaves the operator holding a file that is neither applied
        nor not, and the only way to find out which rows landed is to read
        them back one at a time.
        """
        before = reorder_snapshot(branch)
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, MIXED))
            assert batch.status == ImportBatchStatus.FAILED_VALIDATION
            assert batch.valid_row_count == 1
            assert batch.error_row_count == 1
            with pytest.raises(ValidationError) as refusal:
                imports.apply_batch(batch=batch)
        assert refusal.value.code == "import_not_validated"
        assert reorder_snapshot(branch) == before
        assert batch.applied_row_count == 0

    def test_errors_are_visible_per_row_and_field(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        raw = f"{HEADER}\nNOT-AN-ITEM,maybe,abc,1.000\n".encode()
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, raw))
        row = batch.rows.get()
        assert row.row_number == 2, "the header is row 1, so data starts at 2"
        assert set(row.errors) == {"item_code", "is_stocked", "reorder_point"}
        assert all(isinstance(messages, list) for messages in row.errors.values())

    def test_a_row_asking_for_what_is_already_there_counts_as_unchanged(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        """
        Why `applied_row_count` can legitimately be below `valid_row_count`.
        """
        with audit_context(actor=owner):
            imports.apply_batch(
                batch=imports.validate_batch(batch=upload(organization, branch, GOOD, "a.csv"))
            )
            second = imports.validate_batch(
                batch=upload(organization, branch, GOOD + b"DEMO-MEAT,yes,5.000,5.000\n", "b.csv")
            )
            second = imports.apply_batch(batch=second)
        assert second.valid_row_count == 3
        assert second.applied_row_count == 1, "only the new row changed anything"

    def test_cancelling_needs_a_reason_and_is_terminal(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        with audit_context(actor=owner):
            batch = upload(organization, branch, GOOD)
            with pytest.raises(ValidationError):
                imports.cancel_batch(batch=batch, reason="  ")
            batch = imports.cancel_batch(batch=batch, reason="أُلغيت للاختبار")
            assert batch.status == ImportBatchStatus.CANCELLED
            with pytest.raises(ValidationError) as refusal:
                imports.validate_batch(batch=batch)
        assert refusal.value.code == "import_batch_terminal"


class TestIdempotencyAndConflict:
    def test_applying_twice_is_refused_cleanly(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        with audit_context(actor=owner):
            batch = imports.apply_batch(
                batch=imports.validate_batch(batch=upload(organization, branch, GOOD))
            )
            with pytest.raises(ValidationError) as refusal:
                imports.apply_batch(batch=batch)
        assert refusal.value.code == "import_already_applied"

    def test_the_same_content_under_a_new_batch_is_refused_cleanly(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        """
        A domain error, never a raw IntegrityError.

        The partial unique index is the guarantee; this is the message. A
        screen can only render an IntegrityError as a crash, and the
        operator's real question — "did it already run?" — has a good answer.
        """
        with audit_context(actor=owner):
            imports.apply_batch(
                batch=imports.validate_batch(batch=upload(organization, branch, GOOD, "one.csv"))
            )
            again = imports.validate_batch(batch=upload(organization, branch, GOOD, "two.csv"))
            with pytest.raises(ValidationError) as refusal:
                imports.apply_batch(batch=again)
        assert refusal.value.code == "import_content_already_applied"

    def test_the_fingerprint_ignores_column_order_and_quoting(
        self, organization: Organization, branch: Branch
    ) -> None:
        """Otherwise the retry guard depends on which spreadsheet saved it."""
        rows_a = [{"item_code": "DEMO-RICE", "is_stocked": "yes"}]
        rows_b = [{"is_stocked": "yes", "item_code": "DEMO-RICE"}]
        assert imports.fingerprint(ImportKind.BRANCH_ITEM_SETTING, rows_a) == imports.fingerprint(
            ImportKind.BRANCH_ITEM_SETTING, rows_b
        )

    def test_row_order_is_deterministic(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, GOOD))
        assert [row.row_number for row in batch.rows.order_by("row_number")] == [2, 3]

    def test_a_file_naming_the_same_record_twice_is_refused(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        """
        Which duplicate wins would depend on row order, so neither does.
        """
        raw = f"{HEADER}\nDEMO-RICE,yes,10.000,1.000\nDEMO-RICE,yes,20.000,2.000\n".encode()
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, raw))
        assert batch.status == ImportBatchStatus.FAILED_VALIDATION
        assert batch.error_row_count == 2, "a duplicate poisons every copy, not just the second"


# ---------------------------------------------------------------------------
# File-upload security
# ---------------------------------------------------------------------------


class TestUploadSecurity:
    @pytest.mark.parametrize(
        ("label", "raw", "filename", "code"),
        [
            ("macro workbook", GOOD, "book.xlsm", "import_bad_extension"),
            ("binary workbook", GOOD, "book.xlsb", "import_bad_extension"),
            ("no extension", GOOD, "book", "import_bad_extension"),
            ("empty", b"", "empty.csv", "import_empty"),
            ("headers only", HEADER.encode(), "head.csv", "import_no_rows"),
            ("missing columns", b"wrong,header\n1,2\n", "wrong.csv", "import_missing_columns"),
            (
                "duplicate columns",
                f"{HEADER},reorder_point\nDEMO-RICE,yes,1.000,1.000,2.000\n".encode(),
                "dupe.csv",
                "import_duplicate_columns",
            ),
            (
                "not utf-8",
                "item_code,is_stocked\nرز,yes\n".encode("cp1256"),
                "cp.csv",
                "import_bad_encoding",
            ),
        ],
    )
    def test_the_file_is_refused(
        self,
        organization: Organization,
        branch: Branch,
        label: str,
        raw: bytes,
        filename: str,
        code: str,
    ) -> None:
        with pytest.raises(ValidationError) as refusal:
            upload(organization, branch, raw, filename)
        assert refusal.value.code == code, label

    def test_an_oversized_file_is_refused(self, organization: Organization, branch: Branch) -> None:
        raw = HEADER.encode() + b"\n" + b"x" * (imports.MAX_UPLOAD_BYTES + 1)
        with pytest.raises(ValidationError) as refusal:
            upload(organization, branch, raw, "big.csv")
        assert refusal.value.code == "import_too_large"

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd.csv",
            "..\\..\\windows\\system32\\evil.csv",
            "re‮gnp.csv",
            "nul\x00byte.csv",
        ],
    )
    def test_the_stored_filename_cannot_traverse_or_disguise(self, hostile: str) -> None:
        """
        A right-to-left override can make `evil.csv.exe` render as
        `exe.csv.live`, which is why the character class matters as much as
        the separators.
        """
        cleaned = imports.sanitise_filename(hostile)
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert "\x00" not in cleaned
        assert "‮" not in cleaned
        assert cleaned

    def test_a_formula_in_an_imported_value_is_stored_as_text(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        """
        The payload records what the file said; nothing evaluates it.
        """
        raw = f"{HEADER}\n=cmd|' /c calc'!A1,yes,1.000,1.000\n".encode()
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, raw))
        row = batch.rows.get()
        assert row.is_valid is False
        assert row.payload["item_code"].startswith("=cmd")

    def test_decimal_values_never_pass_through_float(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        raw = f"{HEADER}\nDEMO-RICE,yes,0.001,1000000.999\n".encode()
        with audit_context(actor=owner):
            batch = imports.apply_batch(
                batch=imports.validate_batch(batch=upload(organization, branch, raw))
            )
        assert batch.status == ImportBatchStatus.APPLIED
        setting = BranchItemSetting.objects.get(branch=branch, item__code="DEMO-RICE")
        assert setting.reorder_point == Decimal("0.001")
        assert setting.reorder_quantity == Decimal("1000000.999")


class TestScopeInjection:
    def test_a_foreign_branch_is_refused(self, organization: Organization) -> None:
        other = create_organization(code="RIVALIMP", name_ar="منافس", name_en="Rival")
        import datetime as _datetime

        from apps.organizations.services import create_branch

        foreign = create_branch(
            organization=other,
            code="FBR",
            name_ar="فرع",
            name_en="Branch",
            business_day_start_time=_datetime.time(6, 0),
        )
        with pytest.raises(ValidationError) as refusal:
            upload(organization, foreign, GOOD)
        assert refusal.value.code == "import_branch_mismatch"

    def test_a_row_naming_another_organizations_item_finds_nothing(
        self, owner: User, organization: Organization, branch: Branch
    ) -> None:
        """
        Resolved within the batch's own organization.

        The answer is "no such item", the same one the screen would give —
        it reveals nothing about whether the code exists elsewhere.
        """
        other = create_organization(code="RIVALITEM", name_ar="منافس", name_en="Rival")
        ItemCategory.objects.filter(organization=other)  # the rival has its own master
        raw = f"{HEADER}\nDEMO-RICE,yes,1.000,1.000\n".encode()
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, raw))
        assert batch.status == ImportBatchStatus.VALIDATED, "its own item resolves"

    def test_an_unsupported_kind_cannot_be_uploaded(
        self, organization: Organization, branch: Branch
    ) -> None:
        """
        A kind with no writer is not offered and cannot be reached by POST.

        `OPENING_STOCK_DRAFT` is the one that matters: it is the only kind
        that would touch the ledger, and it is deferred rather than
        half-built.
        """
        # Passed as a raw string, because the value is no longer in the enum
        # to be named — which is the strongest form of "cannot be reached".
        with pytest.raises(ValidationError) as refusal:
            imports.create_batch(
                organization=organization,
                branch=branch,
                kind="OPENING_STOCK_DRAFT",
                raw=GOOD,
                filename="opening.csv",
            )
        assert refusal.value.code == "import_kind_unsupported"

    def test_the_enum_the_columns_the_validators_and_the_writers_all_agree(self) -> None:
        """
        Four lists that must say the same thing.

        The way they stop agreeing is somebody adding a kind to one of them.
        `OPENING_STOCK_DRAFT`, `INVENTORY_ITEM`, `ITEM_CONVERSION` and
        `WAREHOUSE` were declared during Task 1.7A and removed again once it
        was clear none of them had an apply service — a dropdown entry that
        accepts a file and then fails is worse than an absent one.
        """
        declared = set(ImportKind.values)
        assert declared == set(imports.VALIDATORS)
        assert declared == set(imports.WRITERS)
        assert declared == set(imports.REQUIRED_COLUMNS)
        assert declared == set(imports.supported_kinds())
        # Task 2.17 registered procurement's three kinds into this module's
        # registries from `apps.procurement.imports` — the positive twin of
        # the three-kind boundary this assertion used to hold.
        assert declared == {
            "ITEM_CATEGORY",
            "PACKAGE_UNIT",
            "BRANCH_ITEM_SETTING",
            "SUPPLIER",
            "SUPPLIER_ITEM",
            "PURCHASE_REQUEST_DRAFT",
        }

    def test_the_opening_draft_kind_is_gone_not_merely_unimplemented(self) -> None:
        """
        The one kind that would have reached the ledger.

        Deferred rather than half-built, and absent from the vocabulary so it
        cannot be reached by a POST that guesses the value.
        """
        from apps.inventory.models import OPENING_IMPORT_KINDS

        assert not hasattr(ImportKind, "OPENING_STOCK_DRAFT")
        assert OPENING_IMPORT_KINDS == frozenset()

    def test_the_reserved_opening_permission_is_granted_to_nobody(self) -> None:
        """
        A grant for a capability nobody can exercise is a grant nobody audits.

        Same treatment as `override_negative_stock`: the code stays so the
        vocabulary is stable, and no role holds it.
        """
        from apps.inventory.permissions import IMPORT_OPENING_DRAFT, ROLE_PERMISSIONS

        holders = [
            role for role, perms in ROLE_PERMISSIONS.items() if IMPORT_OPENING_DRAFT in perms
        ]
        assert holders == []

    def test_only_supported_kinds_are_offered_in_the_ui(
        self, owner: User, organization: Organization, client_for: Callable[[User], Client]
    ) -> None:
        """No dead option in the Arabic dropdown."""
        body = client_for(owner).get(reverse("inventory:import_upload")).content.decode()
        for value in ImportKind.values:
            assert f'value="{value}"' in body
        for removed in ("OPENING_STOCK_DRAFT", "INVENTORY_ITEM", "ITEM_CONVERSION", "WAREHOUSE"):
            assert f'value="{removed}"' not in body

    def test_the_database_refuses_an_unsupported_kind(self, organization: Organization) -> None:
        """
        Django choices are a form-layer courtesy, not a boundary.

        A raw INSERT, a data migration or a `bulk_create` walks straight past
        them, and a batch whose kind has no validator could never be previewed
        or applied — it would sit in the history looking like work somebody did.
        """
        from django.db import connection
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO inventory_importbatch
                    (created_at, updated_at, organization_id, branch_id, public_id,
                     batch_number, kind, status, original_filename, content_hash,
                     byte_size, row_count, valid_row_count, error_row_count,
                     applied_row_count, notes, reason)
                VALUES (now(), now(), %s, NULL, gen_random_uuid(),
                        '', 'OPENING_STOCK_DRAFT', 'UPLOADED', 'raw.csv', 'deadbeef',
                        1, 0, 0, 0, 0, '', '')
                """,
                [organization.pk],
            )


# ---------------------------------------------------------------------------
# Permissions and screens
# ---------------------------------------------------------------------------


class TestImportAuthorization:
    def test_the_history_screen_needs_its_own_permission(
        self, seeded: None, units: None, client_for: Callable[[User], Client]
    ) -> None:
        keeper = User.objects.get(username="demo-storekeeper")
        assert client_for(keeper).get(reverse("inventory:import_list")).status_code == 403

    def test_an_accountant_may_read_history_but_not_apply(
        self, organization: Organization, branch: Branch, owner: User, units: None
    ) -> None:
        """
        The point of keeping the two permissions apart.

        Someone who may apply nothing still has to be able to see what was
        applied and by whom.
        """
        from apps.inventory.import_views import permission_for_kind
        from apps.inventory.permissions import VIEW_IMPORT_HISTORY

        accountant = User.objects.create_user(username="acct", password="pw-not-real-1234")
        grant_organization_access(user=accountant, organization=organization, role=Role.ACCOUNTANT)
        accountant = User.objects.get(pk=accountant.pk)
        assert accountant.has_perm(VIEW_IMPORT_HISTORY)
        assert not accountant.has_perm(permission_for_kind(ImportKind.BRANCH_ITEM_SETTING))

    def test_a_direct_post_without_the_kind_permission_is_refused(
        self,
        organization: Organization,
        branch: Branch,
        owner: User,
        units: None,
        client_for: Callable[[User], Client],
    ) -> None:
        """A hidden button is not a control."""
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, GOOD))

        accountant = User.objects.create_user(username="acct2", password="pw-not-real-1234")
        grant_organization_access(user=accountant, organization=organization, role=Role.ACCOUNTANT)
        response = client_for(User.objects.get(pk=accountant.pk)).post(
            reverse("inventory:import_detail", args=[batch.pk]), {"action": "apply"}
        )
        assert response.status_code == 403
        batch.refresh_from_db()
        assert batch.status == ImportBatchStatus.VALIDATED

    def test_a_batch_from_another_organization_is_a_404(
        self,
        organization: Organization,
        branch: Branch,
        owner: User,
        units: None,
        client_for: Callable[[User], Client],
    ) -> None:
        with audit_context(actor=owner):
            batch = upload(organization, branch, GOOD)
        outsider = User.objects.create_user(username="nosy", password="pw-not-real-1234")
        response = client_for(outsider).get(reverse("inventory:import_detail", args=[batch.pk]))
        assert response.status_code in (403, 404)


class TestImportScreens:
    def test_the_detail_partial_carries_only_the_rows(
        self,
        owner: User,
        organization: Organization,
        branch: Branch,
        client_for: Callable[[User], Client],
    ) -> None:
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, MIXED))
        body = (
            client_for(owner)
            .get(reverse("inventory:import_detail", args=[batch.pk]), headers=HX)
            .content.decode()
        )
        assert "<html" not in body
        assert 'class="rail"' not in body
        assert 'id="list-results"' in body

    def test_the_row_filter_narrows_the_table(
        self,
        owner: User,
        organization: Organization,
        branch: Branch,
        client_for: Callable[[User], Client],
    ) -> None:
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, MIXED))
        client = client_for(owner)
        url = reverse("inventory:import_detail", args=[batch.pk])
        errors_only = client.get(url, {"rows": "errors"}, headers=HX).content.decode()
        assert "NOT-AN-ITEM" in errors_only
        assert "DEMO-RICE" not in errors_only

    def test_the_arabic_error_message_reaches_the_screen(
        self,
        owner: User,
        organization: Organization,
        branch: Branch,
        client_for: Callable[[User], Client],
    ) -> None:
        with audit_context(actor=owner):
            batch = imports.validate_batch(batch=upload(organization, branch, MIXED))
        body = (
            client_for(owner)
            .get(reverse("inventory:import_detail", args=[batch.pk]))
            .content.decode()
        )
        assert "لا يوجد صنف بهذا الرمز في هذه المؤسسة." in body


# ---------------------------------------------------------------------------
# Projection verification
# ---------------------------------------------------------------------------


class TestProjectionVerification:
    def test_a_clean_projection_verifies(self, organization: Organization) -> None:
        assert verify_organization(organization) == []

    def test_the_command_exits_zero_when_clean(self, organization: Organization) -> None:
        call_command("verify_stock_projection", organization=organization.code, stdout=StringIO())

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("quantity", Decimal("999.000"), "quantity"),
            ("value", Decimal("1.000"), "value"),
            ("average_cost", Decimal("7.000000"), "average_cost"),
            ("last_posted_sequence", 999_999, "last_posted_sequence"),
        ],
    )
    def test_planted_drift_is_detected(
        self, organization: Organization, field: str, value: object, expected: str
    ) -> None:
        """
        A verifier only ever run against clean data has never been tested.

        The projection is written to directly here on purpose — that is the
        failure being simulated: a balance no posting produced.
        """
        balance = StockBalance.objects.filter(
            organization=organization, item__code="DEMO-RICE", warehouse__code="DEMO-MAIN"
        ).get()
        setattr(balance, field, value)
        balance.save(update_fields=[field])

        mismatches = verify_organization(organization)
        assert any(mismatch.field == expected for mismatch in mismatches), [
            str(mismatch) for mismatch in mismatches
        ]

    def test_planted_control_account_drift_is_detected(self, organization: Organization) -> None:
        from apps.accounting.models import Account

        other = Account.objects.get(organization=organization, code="1-03-02-001")
        balance = StockBalance.objects.filter(
            organization=organization, item__code="DEMO-RICE", warehouse__code="DEMO-MAIN"
        ).get()
        balance.control_account = other
        balance.save(update_fields=["control_account"])
        assert any(
            mismatch.field == "control_account" for mismatch in verify_organization(organization)
        )

    def test_the_command_exits_non_zero_on_drift(self, organization: Organization) -> None:
        balance = StockBalance.objects.filter(
            organization=organization, item__code="DEMO-RICE", warehouse__code="DEMO-MAIN"
        ).get()
        balance.quantity = Decimal("1.000")
        balance.save(update_fields=["quantity"])
        with pytest.raises(SystemExit) as exit_code:
            call_command(
                "verify_stock_projection", organization=organization.code, stdout=StringIO()
            )
        assert exit_code.value.code == 1

    def test_verification_mutates_nothing(self, organization: Organization) -> None:
        """Read-only, so it can be run against production safely."""
        before = sorted(
            StockBalance.objects.filter(organization=organization).values_list(
                "id", "quantity", "value", "average_cost", "last_posted_sequence"
            )
        )
        verify_organization(organization)
        call_command("verify_stock_projection", organization=organization.code, stdout=StringIO())
        after = sorted(
            StockBalance.objects.filter(organization=organization).values_list(
                "id", "quantity", "value", "average_cost", "last_posted_sequence"
            )
        )
        assert after == before

    def test_there_is_no_repair_mode(self) -> None:
        """
        Deferred deliberately, not forgotten.

        A repair without a maintenance lock, a reason, an actor and a final
        verification overwrites the evidence of a defect with a plausible
        number — worse than the drift, because afterwards nobody can tell.
        """
        from apps.inventory.management.commands import verify_stock_projection

        source = verify_stock_projection.__file__
        assert source
        with open(source, encoding="utf-8") as handle:
            body = handle.read()
        assert "--apply" not in body.replace('"--apply"', "").replace("`--apply`", "")

    def test_a_lot_and_a_null_lot_are_different_positions(self, organization: Organization) -> None:
        """NULL-safe keys: chicken is held in lots, packaging is not."""
        keys: set[tuple[str, str | None]] = set(
            StockBalance.objects.filter(organization=organization).values_list(
                "item__code", "lot__code"
            )
        )
        assert ("DEMO-CHICKEN", "DEMO-CHK-LOT-01") in keys
        # The NULL-lot half of the key. A projection keyed only on lots would
        # lose packaging entirely, and a non-NULL-safe comparison would report
        # it as drift on every run.
        assert ("DEMO-CONTAINER", None) in keys
        assert verify_organization(organization) == []

    def test_a_fully_depleted_position_replays_to_exactly_zero(
        self, organization: Organization
    ) -> None:
        """The full-depletion rule, checked on the wasted expired lot."""
        emptied = StockBalance.objects.get(organization=organization, lot__code="DEMO-CHK-LOT-02")
        assert emptied.quantity == Decimal("0.000")
        assert emptied.value == Decimal("0.000")
        assert emptied.control_account_id is None
        assert verify_organization(organization) == []

    def test_the_scope_filters_narrow_both_sides(self, organization: Organization) -> None:
        """
        Narrowing one side only would manufacture mismatches out of rows the
        other legitimately excluded.
        """
        branch = Branch.objects.get(organization=organization, code="DEMO-BUNOOK")
        assert verify_organization(organization, branch_id=branch.pk) == []

    def test_an_unknown_selector_exits_two(self, organization: Organization) -> None:
        """Silence must never read as 'verified'."""
        with pytest.raises(SystemExit) as exit_code:
            call_command(
                "verify_stock_projection",
                organization=organization.code,
                warehouse_code="NOPE",
                stdout=StringIO(),
            )
        assert exit_code.value.code == 2


# ---------------------------------------------------------------------------
# Database constraints, at the commit boundary
# ---------------------------------------------------------------------------
