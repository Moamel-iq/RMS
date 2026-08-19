"""
Sales routes.

Routes arrive with the screens behind them. A named route with no view is a
navigation entry that 404s, which is worse than an obviously unfinished one —
so the twelve sidebar sections are activated one checkpoint at a time, each
only after its route answers 200 both as a full page and as an htmx fragment.

Checkpoint 1: أصناف المنيو and قنوات البيع, plus the price and category
screens they need.
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
]
