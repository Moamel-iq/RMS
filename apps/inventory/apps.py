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

        # Registered at ready so the rule holds no matter which screen or
        # service changes an organization default: an INVENTORY_CONTROL
        # mapping change that would re-home standing stock value is refused
        # (ADR-019 §G). Accounting exposes the hook; it never imports us.
        from apps.accounting.services import register_mapping_guard, register_period_close_guard
        from apps.inventory.accounts import organization_mapping_guard
        from apps.inventory.counts import refuse_close_while_a_count_is_active

        register_mapping_guard(organization_mapping_guard)

        # And for the same reason in the other direction: a period closed while
        # a physical count is still open would strand a frozen warehouse that
        # can neither post nor be reopened without an authorized period
        # reopening (ADR-021 §9).
        register_period_close_guard(refuse_close_while_a_count_is_active)
