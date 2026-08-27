from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


def _sync_role_groups(sender: object, **kwargs: object) -> None:
    """Give each role its automation capabilities after migrations install them."""

    from apps.core.permissions import sync_role_groups

    sync_role_groups()


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = _("Core")

    def ready(self) -> None:
        post_migrate.connect(_sync_role_groups, sender=self)
