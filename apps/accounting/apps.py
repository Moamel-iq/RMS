from typing import Any

from django.apps import AppConfig
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.db.models.signals import post_migrate
from django.db.utils import OperationalError, ProgrammingError
from django.utils.translation import gettext_lazy as _


def _account_role_history_is_ready() -> bool:
    """Avoid emitting history until the unified-name schema is fully installed."""
    try:
        return (
            MigrationRecorder(connection)
            .migration_qs.filter(
                app="accounting",
                name="0025_remove_account_account_names_not_empty_and_more",
            )
            .exists()
        )
    except OperationalError, ProgrammingError:
        return False


def _sync_role_groups(sender: Any, **kwargs: Any) -> None:
    """
    Keep the role groups in step with the permission table after every migrate.

    Bound to `post_migrate` because permissions do not exist until their
    migration has run, and because a deployment that adds a permission and
    forgets to hand it to a role produces the worst kind of failure: an
    authority nobody holds, discovered by an accountant who cannot close a
    period.

    The system account-role vocabulary is re-asserted here too: a test-suite
    flush truncates data-migration rows and replays only post_migrate, and a
    database without `INVENTORY_CONTROL` cannot post an opening.
    """
    if not _account_role_history_is_ready():
        return

    from apps.accounting.permissions import sync_role_groups
    from apps.accounting.services import sync_system_account_roles

    sync_system_account_roles()
    sync_role_groups()


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"
    label = "accounting"
    verbose_name = _("Accounting")

    def ready(self) -> None:
        post_migrate.connect(_sync_role_groups, sender=self)
