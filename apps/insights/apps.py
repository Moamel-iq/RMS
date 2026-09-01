from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


def _sync_role_groups(sender: object, **kwargs: object) -> None:
    """Install the insights grants once the permissions exist."""
    from apps.insights.permissions import sync_role_groups

    sync_role_groups()


class InsightsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.insights"
    label = "insights"
    verbose_name = _("Insights")

    def ready(self) -> None:
        # Importing the module is what registers the detector: the registry is
        # populated by import side effect, so a detector nobody imports is a
        # detector that silently does not exist.
        from apps.insights.detectors import inventory_issue_coverage  # noqa: F401

        post_migrate.connect(_sync_role_groups, sender=self)
