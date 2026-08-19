"""
Sales routes.

Routes arrive with the screens behind them. A named route with no view is a
navigation entry that 404s, which is worse than an obviously unfinished one —
so the twelve sidebar sections are activated one checkpoint at a time, each
only after its route answers 200 both as a full page and as an htmx fragment.

Checkpoint 1: أصناف المنيو and قنوات البيع, plus the price and category
screens they need.

Checkpoint 2: تطبيقات التوصيل, العمولات والاتفاقيات and الخصومات.
"""

from __future__ import annotations

from django.urls import URLPattern, path

from apps.sales import views

app_name = "sales"

urlpatterns: list[URLPattern] = [
    # --- أصناف المنيو ------------------------------------------------------
    path("menu-items/", views.MenuItemListView.as_view(), name="menu_item_list"),
    path("menu-items/new/", views.MenuItemCreateView.as_view(), name="menu_item_create"),
    path("menu-items/<int:pk>/", views.MenuItemDetailView.as_view(), name="menu_item_detail"),
    path("menu-items/<int:pk>/edit/", views.MenuItemUpdateView.as_view(), name="menu_item_update"),
    path(
        "menu-items/<int:pk>/archive/",
        views.MenuItemActionView.as_view(activate=False),
        name="menu_item_archive",
    ),
    path(
        "menu-items/<int:pk>/reactivate/",
        views.MenuItemActionView.as_view(activate=True),
        name="menu_item_reactivate",
    ),
    # --- مجموعات المنيو ----------------------------------------------------
    path("menu-categories/", views.MenuCategoryListView.as_view(), name="menu_category_list"),
    path(
        "menu-categories/new/",
        views.MenuCategoryCreateView.as_view(),
        name="menu_category_create",
    ),
    path(
        "menu-categories/<int:pk>/edit/",
        views.MenuCategoryUpdateView.as_view(),
        name="menu_category_update",
    ),
    # --- أسعار المنيو ------------------------------------------------------
    path("menu-prices/", views.MenuPriceListView.as_view(), name="menu_price_list"),
    path("menu-prices/new/", views.MenuPriceCreateView.as_view(), name="menu_price_create"),
    # Closing a price is a form (it needs a date and a reason); archiving one
    # is a POST-only action. Two different acts, and the difference is real:
    # closing says "this was right until Tuesday", archiving says "this was
    # never right".
    path(
        "menu-prices/<int:pk>/close/", views.MenuPriceCloseView.as_view(), name="menu_price_close"
    ),
    path(
        "menu-prices/<int:pk>/archive/",
        views.MenuPriceArchiveView.as_view(activate=False),
        name="menu_price_archive",
    ),
    # --- قنوات البيع -------------------------------------------------------
    path("channels/", views.SalesChannelListView.as_view(), name="channel_list"),
    path("channels/new/", views.SalesChannelCreateView.as_view(), name="channel_create"),
    path("channels/<int:pk>/edit/", views.SalesChannelUpdateView.as_view(), name="channel_update"),
    path(
        "channels/<int:pk>/archive/",
        views.SalesChannelActionView.as_view(activate=False),
        name="channel_archive",
    ),
    path(
        "channels/<int:pk>/reactivate/",
        views.SalesChannelActionView.as_view(activate=True),
        name="channel_reactivate",
    ),
    # --- تطبيقات التوصيل ---------------------------------------------------
    path("applications/", views.DeliveryApplicationListView.as_view(), name="application_list"),
    path(
        "applications/new/",
        views.DeliveryApplicationCreateView.as_view(),
        name="application_create",
    ),
    path(
        "applications/<int:pk>/",
        views.DeliveryApplicationDetailView.as_view(),
        name="application_detail",
    ),
    path(
        "applications/<int:pk>/edit/",
        views.DeliveryApplicationUpdateView.as_view(),
        name="application_update",
    ),
    path(
        "applications/<int:pk>/archive/",
        views.DeliveryApplicationActionView.as_view(activate=False),
        name="application_archive",
    ),
    path(
        "applications/<int:pk>/reactivate/",
        views.DeliveryApplicationActionView.as_view(activate=True),
        name="application_reactivate",
    ),
    # --- العمولات والاتفاقيات ----------------------------------------------
    #
    # No edit route, and its absence is the design: a rate that has accrued a
    # commission is evidence, so the only correction is ending the agreement
    # and recording the replacement.
    path("agreements/", views.DeliveryAgreementListView.as_view(), name="agreement_list"),
    path("agreements/new/", views.DeliveryAgreementCreateView.as_view(), name="agreement_create"),
    path(
        "agreements/<int:pk>/close/",
        views.DeliveryAgreementCloseView.as_view(),
        name="agreement_close",
    ),
    # --- الخصومات ----------------------------------------------------------
    path("discounts/", views.DiscountProgramListView.as_view(), name="discount_list"),
    path("discounts/new/", views.DiscountProgramCreateView.as_view(), name="discount_create"),
    path(
        "discounts/<int:pk>/close/",
        views.DiscountProgramCloseView.as_view(),
        name="discount_close",
    ),
]
