from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


def _sync_role_groups(sender: Any, **kwargs: Any) -> None:
    from apps.hr.permissions import sync_role_groups

    sync_role_groups()


class HumanResourcesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.hr"
    label = "hr"
    verbose_name = _("Human Resources and Payroll")

    def ready(self) -> None:
        post_migrate.connect(_sync_role_groups, sender=self)
