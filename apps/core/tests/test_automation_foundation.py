"""Regression tests for the durable automation foundation."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import time

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.automation import (
    HANDLERS,
    MAX_ATTEMPTS,
    acknowledge_task,
    claim_next_event,
    enqueue_event,
    open_exception,
    outbox_metrics,
    process_due_events,
    register_handler,
    replay_dead_letter,
    tasks_for_actor,
)
from apps.core.context import audit_context
from apps.core.models import (
    AutomationDataSensitivity,
    AutomationException,
    AutomationOutboxAttempt,
    AutomationOutboxEvent,
    AutomationSeverity,
    AutomationTask,
    AutomationTaskStatus,
    OutboxEventStatus,
)
from apps.core.permissions import sync_role_groups
from apps.organizations.models import Branch, Organization, Role
from apps.organizations.services import (
    create_branch,
    create_organization,
    grant_branch_access,
    grant_organization_access,
)
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _sync_core_permissions_for_reused_database() -> None:
    """`--reuse-db` can predate a newly added model permission's post_migrate."""

    sync_role_groups()


@pytest.fixture
def organization() -> Organization:
    return create_organization(code="AUTO", name="أتمتة")


@pytest.fixture
def other_organization() -> Organization:
    return create_organization(code="OTHER", name="أخرى")


@pytest.fixture
def branch(organization: Organization) -> Branch:
    return create_branch(
        organization=organization,
        code="MAIN",
        name="الرئيسي",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def other_branch(other_organization: Organization) -> Branch:
    return create_branch(
        organization=other_organization,
        code="OTHER",
        name="الآخر",
        business_day_start_time=time(9, 0),
    )


@pytest.fixture
def accounting_manager(organization: Organization) -> User:
    user = User.objects.create_user(username="automation-finance", password="pw-not-real")
    grant_organization_access(user=user, organization=organization, role=Role.ACCOUNTING_MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def branch_manager(branch: Branch) -> User:
    user = User.objects.create_user(username="automation-manager", password="pw-not-real")
    grant_branch_access(user=user, branch=branch, role=Role.MANAGER)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def outsider(other_organization: Organization) -> User:
    user = User.objects.create_user(username="automation-outsider", password="pw-not-real")
    grant_organization_access(
        user=user, organization=other_organization, role=Role.ACCOUNTING_MANAGER
    )
    return User.objects.get(pk=user.pk)


def _event(
    *, organization: Organization, branch: Branch | None = None, event_type: str | None = None
) -> AutomationOutboxEvent:
    return enqueue_event(
        organization=organization,
        branch=branch,
        event_type=event_type or f"test.event.{uuid.uuid4()}",
        idempotency_key="economic-event-1",
        payload={"reference": "E-1"},
        source=organization,
    )


def test_a_rolled_back_business_transaction_leaves_no_outbox_event(
    organization: Organization,
) -> None:
    with pytest.raises(RuntimeError), transaction.atomic():
        _event(organization=organization)
        raise RuntimeError("simulate rejected business change")

    assert AutomationOutboxEvent.objects.count() == 0


def test_same_event_key_is_idempotent_and_conflicting_content_is_refused(
    organization: Organization,
) -> None:
    event_type = f"test.duplicate.{uuid.uuid4()}"
    first = _event(organization=organization, event_type=event_type)
    replay = _event(organization=organization, event_type=event_type)

    assert replay.pk == first.pk
    with pytest.raises(ValidationError) as conflict:
        enqueue_event(
            organization=organization,
            event_type=event_type,
            idempotency_key="economic-event-1",
            payload={"reference": "different"},
            source=organization,
        )
    assert conflict.value.code == "automation_event_idempotency_conflict"


def test_a_retryable_handler_deduplicates_its_result(
    organization: Organization,
    branch: Branch,
) -> None:
    event_type = f"test.idempotent-handler.{uuid.uuid4()}"

    @register_handler(event_type)
    def idempotent_handler(event: AutomationOutboxEvent) -> None:
        open_exception(
            organization=event.organization,
            branch=event.branch,
            code="test_source_gap",
            target=event.organization,
            severity=AutomationSeverity.HIGH,
            is_blocking=True,
            sensitivity=AutomationDataSensitivity.FINANCIAL,
            owner_role=Role.ACCOUNTING_MANAGER,
            source_event=event,
            title="فجوة مصدر اختبارية",
        )

    event = _event(organization=organization, branch=branch, event_type=event_type)
    outcome = process_due_events(limit=1)
    assert outcome["succeeded"] == 1

    # This models delivery after a worker crash between a committed handler and
    # its completion flag. The handler itself has to be safe when it sees the
    # same durable event again.
    HANDLERS[event_type](event)
    assert AutomationException.objects.filter(code="test_source_gap").count() == 1
    assert AutomationTask.objects.filter(task_type="test_source_gap").count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_workers_cannot_claim_the_same_message(
    organization: Organization,
) -> None:
    event = _event(organization=organization)

    def claim() -> int | None:
        close_old_connections()
        try:
            claimed = claim_next_event(worker_id=uuid.uuid4())
            return claimed.pk if claimed else None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: claim(), range(2)))

    assert sorted(value for value in claimed if value is not None) == [event.pk]
    event.refresh_from_db()
    assert event.status == OutboxEventStatus.PROCESSING
    assert AutomationOutboxAttempt.objects.filter(event=event).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_detectors_create_one_open_exception_and_one_task(
    organization: Organization,
    branch: Branch,
) -> None:
    def detect() -> int:
        close_old_connections()
        try:
            exception = open_exception(
                organization=organization,
                branch=branch,
                code="test_concurrent_condition",
                target=organization,
                severity=AutomationSeverity.HIGH,
                is_blocking=True,
                sensitivity=AutomationDataSensitivity.FINANCIAL,
                owner_role=Role.ACCOUNTING_MANAGER,
                title="استثناء متزامن",
            )
            return exception.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        identifiers = list(pool.map(lambda _: detect(), range(2)))

    assert len(set(identifiers)) == 1
    assert AutomationException.objects.filter(code="test_concurrent_condition").count() == 1
    assert AutomationTask.objects.filter(task_type="test_concurrent_condition").count() == 1


def test_failures_retry_then_become_a_visible_dead_letter(
    organization: Organization,
    accounting_manager: User,
) -> None:
    event_type = f"test.always-fails.{uuid.uuid4()}"

    @register_handler(event_type)
    def always_fails(event: AutomationOutboxEvent) -> None:
        raise RuntimeError("deliberate test failure")

    event = _event(organization=organization, event_type=event_type)
    for _ in range(MAX_ATTEMPTS):
        process_due_events(limit=1)
        event.refresh_from_db()
        if event.status != OutboxEventStatus.DEAD_LETTER:
            event.available_at = timezone.now()
            event.save(update_fields=["available_at", "updated_at"])

    event.refresh_from_db()
    assert event.status == OutboxEventStatus.DEAD_LETTER
    assert event.attempt_count == MAX_ATTEMPTS
    assert event.last_error
    assert AutomationOutboxAttempt.objects.filter(event=event).count() == MAX_ATTEMPTS

    with audit_context(actor=accounting_manager):
        replayed = replay_dead_letter(event=event, actor=accounting_manager)
    assert replayed.status == OutboxEventStatus.PENDING


def test_task_inbox_is_organization_branch_role_and_sensitivity_scoped(
    organization: Organization,
    branch: Branch,
    accounting_manager: User,
    branch_manager: User,
    outsider: User,
) -> None:
    with audit_context(actor=accounting_manager):
        exception = open_exception(
            organization=organization,
            branch=branch,
            code="test_daily_close_gap",
            target=organization,
            severity=AutomationSeverity.HIGH,
            is_blocking=True,
            sensitivity=AutomationDataSensitivity.FINANCIAL,
            owner_role=Role.ACCOUNTING_MANAGER,
            title="مهمة مالية ضمن النطاق",
        )
    task = exception.tasks.get()

    assert list(tasks_for_actor(actor=accounting_manager).values_list("pk", flat=True)) == [task.pk]
    assert not tasks_for_actor(actor=branch_manager).exists()
    assert not tasks_for_actor(actor=outsider).exists()

    owner_client = Client()
    owner_client.force_login(accounting_manager)
    manager_client = Client()
    manager_client.force_login(branch_manager)
    outsider_client = Client()
    outsider_client.force_login(outsider)
    inbox_url = reverse("core:task_inbox")
    assert task.title.encode() in owner_client.get(inbox_url).content
    assert task.title.encode() not in manager_client.get(inbox_url).content
    assert task.title.encode() not in outsider_client.get(inbox_url).content

    with audit_context(actor=accounting_manager):
        acknowledged = acknowledge_task(task=task, actor=accounting_manager)
    assert acknowledged.status == AutomationTaskStatus.ACKNOWLEDGED
    assert acknowledged.acknowledged_by_id == accounting_manager.pk


def test_database_scope_guard_refuses_a_cross_organization_branch_pair(
    organization: Organization,
    other_branch: Branch,
) -> None:
    with pytest.raises(Exception, match="must belong to its organization"), transaction.atomic():
        AutomationOutboxEvent.objects.create(
            organization=organization,
            branch=other_branch,
            event_type="test.illegal-scope",
            idempotency_key="illegal",
            payload={},
            payload_hash="0" * 64,
            correlation_id=uuid.uuid4(),
        )


def test_outbox_monitoring_metrics_are_scoped_to_the_selected_organizations(
    organization: Organization,
    other_organization: Organization,
) -> None:
    _event(organization=organization)
    _event(organization=other_organization)

    metrics = outbox_metrics(organizations=Organization.objects.filter(pk=organization.pk))

    assert metrics["pending"] == 1
    assert metrics["completed"] == 0
