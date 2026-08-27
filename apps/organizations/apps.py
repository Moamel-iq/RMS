from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


def _sync_role_groups(sender: object, **kwargs: object) -> None:
    """Install the security-administration grants after permissions exist."""
    from apps.organizations.security_permissions import sync_role_groups

    sync_role_groups()


class OrganizationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.organizations"
    label = "organizations"
    verbose_name = _("Organizations")

    def ready(self) -> None:
        post_migrate.connect(_sync_role_groups, sender=self)
