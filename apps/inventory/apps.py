from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


def _sync_role_groups(sender: Any, **kwargs: Any) -> None:
    """
    Keep the role groups in step with the inventory permission table.

    Same arrangement as `apps.accounting`: permissions do not exist until
    their migration has run, and a deployment that adds a permission without
    handing it to a role produces an authority nobody holds — discovered by a
    storekeeper who cannot receive goods.
    """
    from apps.inventory.permissions import sync_role_groups

    sync_role_groups()


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inventory"
    label = "inventory"
    verbose_name = _("Inventory")

    def ready(self) -> None:
        post_migrate.connect(_sync_role_groups, sender=self)
