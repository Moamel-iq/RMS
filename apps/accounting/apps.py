from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


def _sync_role_groups(sender: Any, **kwargs: Any) -> None:
    """
    Keep the role groups in step with the permission table after every migrate.

    Bound to `post_migrate` because permissions do not exist until their
    migration has run, and because a deployment that adds a permission and
    forgets to hand it to a role produces the worst kind of failure: an
    authority nobody holds, discovered by an accountant who cannot close a
    period.
    """
    from apps.accounting.permissions import sync_role_groups

    sync_role_groups()


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"
    label = "accounting"
    verbose_name = _("Accounting")

    def ready(self) -> None:
        post_migrate.connect(_sync_role_groups, sender=self)
