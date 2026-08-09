"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

from config.api import api

urlpatterns: list[URLPattern | URLResolver] = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("settings/", include("apps.organizations.urls")),
    path("settings/", include("apps.units.urls")),
    path("settings/", include("apps.core.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("accounting/", include("apps.accounting.urls")),
    path("", include("apps.users.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
