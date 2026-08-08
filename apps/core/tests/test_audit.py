"""
Audit foundation.

The trail must be attributable, groupable, immutable, and honest about what it
does not know.
"""

from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.urls import reverse

from apps.core.context import audit_context, get_actor, get_correlation_id
from apps.core.middleware import RESPONSE_HEADER
from apps.core.models import AuditAction, AuditEvent
from apps.core.selectors import audit_trail_for, events_for_actor, events_for_correlation
from apps.core.services import NEVER_SNAPSHOT, record_audit_event, snapshot
from apps.organizations.models import Branch, Role
from apps.organizations.services import create_branch, create_organization, grant_branch_access
from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"


@pytest.fixture
def actor() -> User:
    return User.objects.create_user(username="storekeeper", password=PASSWORD)


@pytest.fixture
def branch() -> Branch:
    organization = create_organization(code="KM", name_ar="خان مندي", name_en="Khan Mandi")
    return create_branch(
        organization=organization,
        code="BUNOOK",
        name_ar="البنوك",
        name_en="Al-Bunook",
        business_day_start_time=time(9, 0),
    )


class TestRecording:
    def test_an_event_is_recorded(self, actor: User, branch: Branch) -> None:
        with audit_context(actor=actor):
            event = record_audit_event(
                action=AuditAction.APPROVED, target=branch, reason="opening the branch"
            )
        assert event.action == AuditAction.APPROVED
        assert event.target_type == "organizations.Branch"
        assert event.target_id == str(branch.pk)
        assert event.reason == "opening the branch"

    def test_the_actor_comes_from_context_not_arguments(self, actor: User, branch: Branch) -> None:
        """
        A caller cannot attribute an action to the wrong user, because there
        is no argument through which to pass one.
        """
        with audit_context(actor=actor):
            event = record_audit_event(action=AuditAction.UPDATED, target=branch)
        assert event.actor == actor

    def test_the_actor_name_is_kept_as_text(self, actor: User, branch: Branch) -> None:
        """
        Renaming a user years later must not change what the trail says about
        what they did.
        """
        with audit_context(actor=actor):
            event = record_audit_event(action=AuditAction.UPDATED, target=branch)
        original_label = event.actor_label
        actor.username = "renamed.person"
        actor.save(update_fields=["username"])
        event.refresh_from_db()
        assert event.actor_label == original_label

    def test_system_actions_record_no_actor(self, branch: Branch) -> None:
        """
        A scheduled job has no user. None is the honest answer and must not be
        faked with a placeholder account.
        """
        with audit_context(actor=None):
            event = record_audit_event(action=AuditAction.POSTED, target=branch)
        assert event.actor is None
        assert event.actor_label == ""

    def test_target_type_is_required(self) -> None:
        with pytest.raises(ValueError):
            record_audit_event(action=AuditAction.UPDATED)


class TestCorrelation:
    def test_events_from_one_unit_of_work_share_a_correlation_id(
        self, actor: User, branch: Branch
    ) -> None:
        with audit_context(actor=actor) as correlation_id:
            record_audit_event(action=AuditAction.CREATED, target=branch)
            record_audit_event(action=AuditAction.APPROVED, target=branch)

        grouped = events_for_correlation(correlation_id)
        assert grouped.count() == 2

    def test_separate_units_of_work_do_not_share_one(self, actor: User, branch: Branch) -> None:
        with audit_context(actor=actor) as first:
            record_audit_event(action=AuditAction.CREATED, target=branch)
        with audit_context(actor=actor) as second:
            record_audit_event(action=AuditAction.UPDATED, target=branch)

        assert first != second
        assert events_for_correlation(first).count() == 1

    def test_a_correlation_id_exists_even_without_a_request(self, branch: Branch) -> None:
        """Work started from a shell is still attributable to a unit of work."""
        event = record_audit_event(action=AuditAction.IMPORTED, target=branch)
        assert isinstance(event.correlation_id, uuid.UUID)

    def test_context_is_restored_after_an_exception(self, actor: User) -> None:
        """A failed import must not leak its actor into whatever runs next."""
        with pytest.raises(RuntimeError), audit_context(actor=actor):
            raise RuntimeError("import blew up")
        assert get_actor() is None

    def test_events_ordered_oldest_first_for_a_correlation(
        self, actor: User, branch: Branch
    ) -> None:
        with audit_context(actor=actor) as correlation_id:
            record_audit_event(action=AuditAction.CREATED, target=branch)
            record_audit_event(action=AuditAction.POSTED, target=branch)
        actions = list(events_for_correlation(correlation_id).values_list("action", flat=True))
        assert actions == [AuditAction.CREATED, AuditAction.POSTED]


class TestImmutability:
    """
    Enforced by a PostgreSQL trigger, so bulk updates, raw SQL, the admin, and
    a psql prompt are all refused — not just the service layer.
    """

    def test_an_event_cannot_be_updated(self, actor: User, branch: Branch) -> None:
        with audit_context(actor=actor):
            event = record_audit_event(action=AuditAction.POSTED, target=branch)

        with pytest.raises(IntegrityError), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).update(reason="rewritten")

    def test_an_event_cannot_be_deleted(self, actor: User, branch: Branch) -> None:
        with audit_context(actor=actor):
            event = record_audit_event(action=AuditAction.POSTED, target=branch)

        with pytest.raises(IntegrityError), transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).delete()

    def test_raw_sql_cannot_rewrite_an_event(self, actor: User, branch: Branch) -> None:
        with audit_context(actor=actor):
            event = record_audit_event(action=AuditAction.POSTED, target=branch)

        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE core_auditevent SET reason = %s WHERE id = %s",
                    ["tampered", event.pk],
                )

    def test_raw_sql_cannot_delete_an_event(self, actor: User, branch: Branch) -> None:
        with audit_context(actor=actor):
            event = record_audit_event(action=AuditAction.POSTED, target=branch)

        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM core_auditevent WHERE id = %s", [event.pk])

    def test_an_actor_with_events_cannot_be_deleted(self, actor: User, branch: Branch) -> None:
        """PROTECT: removing the user would orphan the trail."""
        from django.db.models import ProtectedError

        with audit_context(actor=actor):
            record_audit_event(action=AuditAction.POSTED, target=branch)

        with pytest.raises(ProtectedError):
            actor.delete()


class TestSnapshots:
    def test_decimals_are_stored_as_strings_not_floats(self, branch: Branch) -> None:
        """
        JSON would serialise Decimal through float and store
        0.1000000000000000055..., making the audit trail disagree with the
        ledger it audits.
        """
        state = snapshot(branch)
        captured = {key: value for key, value in state.items() if key == "business_day_start_time"}
        assert captured

        from apps.units.models import Dimension, UnitOfMeasure

        unit = UnitOfMeasure.objects.create(
            code="TESTG",
            name_ar="اختبار",
            name_en="Test",
            dimension=Dimension.MASS,
            factor_to_base=Decimal("0.001"),
        )
        assert snapshot(unit)["factor_to_base"] == "0.001"
        assert isinstance(snapshot(unit)["factor_to_base"], str)

    def test_sensitive_fields_are_never_captured(self, actor: User) -> None:
        """A password hash in the audit log outlives every rotation."""
        state = snapshot(actor)
        assert "password" not in state
        for field in NEVER_SNAPSHOT:
            assert field not in state

    def test_before_and_after_are_both_recorded(self, actor: User, branch: Branch) -> None:
        before = snapshot(branch)
        branch.name_en = "Al-Bunook Main"
        branch.save(update_fields=["name_en"])
        after = snapshot(branch)

        with audit_context(actor=actor):
            event = record_audit_event(
                action=AuditAction.UPDATED,
                target=branch,
                previous_state=before,
                new_state=after,
                reason="renamed",
            )

        assert event.previous_state is not None
        assert event.new_state is not None
        assert event.previous_state["name_en"] == "Al-Bunook"
        assert event.new_state["name_en"] == "Al-Bunook Main"


class TestSelectors:
    def test_trail_for_an_object(self, actor: User, branch: Branch) -> None:
        # create_branch records its own CREATED event, so the fixture already
        # contributed one before these two.
        with audit_context(actor=actor):
            record_audit_event(action=AuditAction.APPROVED, target=branch)
            record_audit_event(action=AuditAction.UPDATED, target=branch)
        assert audit_trail_for(branch).count() == 3

    def test_trail_does_not_leak_other_objects(self, actor: User, branch: Branch) -> None:
        other = create_branch(
            organization=branch.organization,
            code="KARRADA",
            name_ar="الكرادة",
            name_en="Karrada",
            business_day_start_time=time(9, 0),
        )
        with audit_context(actor=actor):
            record_audit_event(action=AuditAction.APPROVED, target=branch)

        other_trail = audit_trail_for(other)
        # Only its own creation; nothing recorded against the other branch.
        assert other_trail.count() == 1
        assert {event.target_id for event in other_trail} == {str(other.pk)}
        assert AuditAction.APPROVED not in {event.action for event in other_trail}

    def test_events_for_actor(self, actor: User, branch: Branch) -> None:
        with audit_context(actor=actor):
            record_audit_event(action=AuditAction.CREATED, target=branch)
        assert events_for_actor(actor.pk).count() == 1


class TestRowHistory:
    """
    django-simple-history covers mutable master data. It answers "what did this
    row look like", which is a different question from "what was done".
    """

    def test_master_data_changes_are_historied(self, branch: Branch) -> None:
        assert branch.history.count() == 1
        branch.name_en = "Al-Bunook Main"
        branch.save(update_fields=["name_en"])
        assert branch.history.count() == 2

    def test_previous_values_remain_readable(self, branch: Branch) -> None:
        branch.business_day_start_time = time(10, 0)
        branch.save(update_fields=["business_day_start_time"])
        earliest = branch.history.earliest()
        assert earliest.business_day_start_time == time(9, 0)

    def test_unit_factor_changes_are_historied(self) -> None:
        from apps.units.models import Dimension, UnitOfMeasure

        unit = UnitOfMeasure.objects.create(
            code="HISTG",
            name_ar="غرام",
            name_en="Gram",
            dimension=Dimension.MASS,
            factor_to_base=Decimal("0.001"),
        )
        unit.factor_to_base = Decimal("0.002")
        unit.save(update_fields=["factor_to_base"])
        assert unit.history.earliest().factor_to_base == Decimal("0.001")

    def test_user_history_excludes_the_password(self, actor: User) -> None:
        record = actor.history.first()
        assert not hasattr(record, "password")

    def test_membership_changes_are_historied(self, actor: User, branch: Branch) -> None:
        membership = grant_branch_access(user=actor, branch=branch, role=Role.STOREKEEPER)
        membership.role = Role.MANAGER
        membership.save(update_fields=["role"])
        assert membership.history.count() == 2


class TestRequestContext:
    def test_a_response_carries_a_correlation_id(self, client: Client, actor: User) -> None:
        client.force_login(actor)
        response = client.get(reverse("users:home"))
        assert RESPONSE_HEADER in response
        uuid.UUID(response[RESPONSE_HEADER])

    def test_a_supplied_correlation_id_is_honoured(self, client: Client, actor: User) -> None:
        client.force_login(actor)
        supplied = uuid.uuid4()
        response = client.get(reverse("users:home"), headers={"x-correlation-id": str(supplied)})
        assert response[RESPONSE_HEADER] == str(supplied)

    def test_a_malformed_correlation_id_is_replaced_not_rejected(
        self, client: Client, actor: User
    ) -> None:
        """A bad diagnostic header must not fail the request."""
        client.force_login(actor)
        response = client.get(
            reverse("users:home"), headers={"x-correlation-id": "'; DROP TABLE--"}
        )
        assert response.status_code == 200
        uuid.UUID(response[RESPONSE_HEADER])

    def test_context_does_not_leak_between_requests(self, client: Client, actor: User) -> None:
        client.force_login(actor)
        client.get(reverse("users:home"))
        assert get_actor() is None

    def test_anonymous_requests_record_no_actor(self, client: Client) -> None:
        response = client.get(reverse("users:login"))
        assert response.status_code == 200
        assert get_actor() is None


class TestCorrelationIdHelper:
    def test_generates_one_when_absent(self) -> None:
        assert isinstance(get_correlation_id(), uuid.UUID)

    def test_returns_the_same_id_within_a_context(self) -> None:
        with audit_context():
            assert get_correlation_id() == get_correlation_id()
