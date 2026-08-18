from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


def _sync_role_groups(sender: Any, **kwargs: Any) -> None:
    """
    Keep the role groups in step with the sales permission table.

    Same arrangement as `apps.inventory`, `apps.accounting`, `apps.procurement`
    and `apps.kitchen`: permissions do not exist until their migration has run,
    and a deployment that adds a permission without handing it to a role
    produces an authority nobody holds — discovered by a cashier who cannot
    open their own shift.
    """
    from apps.sales.permissions import sync_role_groups

    sync_role_groups()


class SalesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sales"
    label = "sales"
    verbose_name = _("Sales")

    def ready(self) -> None:
        post_migrate.connect(_sync_role_groups, sender=self)
