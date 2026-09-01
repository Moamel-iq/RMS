"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.db import DatabaseError, connections
from django.http import JsonResponse
from django.urls import URLPattern, URLResolver, include, path

from apps.hr.views import EmployeeDocumentRawMediaBlockView
from apps.supplier_quotes.views import SupplierQuoteRawMediaBlockView
from config.admin import site
from config.api import api


def healthz(_request: object) -> JsonResponse:
    """Report readiness without exposing diagnostics to the public."""
    try:
        connections["default"].ensure_connection()
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


urlpatterns: list[URLPattern | URLResolver] = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", site.urls),
    path("api/v1/", api.urls),
    path("settings/", include("apps.organizations.urls")),
    path("settings/", include("apps.units.urls")),
    path("settings/", include("apps.core.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("procurement/", include("apps.procurement.urls")),
    path("supplier-quotes/", include("apps.supplier_quotes.urls")),
    path("kitchen/", include("apps.kitchen.urls")),
    path("sales/", include("apps.sales.urls")),
    path("accounting/", include("apps.accounting.urls")),
    path("hr/", include("apps.hr.urls")),
    path("insights/", include("apps.insights.urls")),
    # This precedes Django's DEBUG media helper, so a guessed HR attachment
    # URL can never bypass EmployeeDocumentDownloadView's tenant and PII check.
    path(
        "media/hr/employee-documents/<path:path>",
        EmployeeDocumentRawMediaBlockView.as_view(),
    ),
    path(
        "media/supplier-quotes/<path:path>",
        SupplierQuoteRawMediaBlockView.as_view(),
    ),
    path("", include("apps.users.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
