"""
Sales routes.

Routes arrive with the screens behind them. A named route with no view is a
navigation entry that 404s, which is worse than an obviously unfinished one —
so the twelve sidebar sections are activated one checkpoint at a time, each
only after its route answers 200 both as a full page and as an htmx fragment.

Checkpoint 1: أصناف المنيو and قنوات البيع, plus the price and category
screens they need.

Checkpoint 2: تطبيقات التوصيل, العمولات والاتفاقيات and الخصومات.

Checkpoint 3: المبيعات اليومية, and the four lifecycle transitions behind it.

Checkpoint 4: المرتجعات والإلغاءات, and the two transitions behind it.
"""

from __future__ import annotations

from django.urls import URLPattern, path

from apps.sales import adjustment_views, day_views, views

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
    # --- المبيعات اليومية ---------------------------------------------------
    #
    # Four transitions on one view rather than four views: the shape is
    # identical and four copies would be four chances to check the wrong
    # permission.
    path("days/", day_views.SalesDayListView.as_view(), name="day_list"),
    path("days/new/", day_views.SalesDayCreateView.as_view(), name="day_create"),
    path("days/<int:pk>/", day_views.SalesDayDetailView.as_view(), name="day_detail"),
    path(
        "days/<int:pk>/submit/",
        day_views.SalesDayTransitionView.as_view(action="submit"),
        name="day_submit",
    ),
    path(
        "days/<int:pk>/return/",
        day_views.SalesDayTransitionView.as_view(action="return"),
        name="day_return",
    ),
    path(
        "days/<int:pk>/post/",
        day_views.SalesDayTransitionView.as_view(action="post"),
        name="day_post",
    ),
    path(
        "days/<int:pk>/reverse/",
        day_views.SalesDayTransitionView.as_view(action="reverse"),
        name="day_reverse",
    ),
    path(
        "day-lines/<int:pk>/delete/",
        day_views.SalesDayLineDeleteView.as_view(),
        name="day_line_delete",
    ),
    # --- المرتجعات والإلغاءات ------------------------------------------------
    #
    # No edit route on a posted adjustment, and no delete route on one at all.
    # A posted correction has reached the ledger and the application receivable;
    # the only way back is `reverse`, which adds a document rather than removing
    # one — and which needs a different permission from the one that recorded
    # it.
    path(
        "adjustments/", adjustment_views.SalesAdjustmentListView.as_view(), name="adjustment_list"
    ),
    path(
        "adjustments/new/",
        adjustment_views.SalesAdjustmentCreateView.as_view(),
        name="adjustment_create",
    ),
    path(
        "adjustments/<int:pk>/",
        adjustment_views.SalesAdjustmentDetailView.as_view(),
        name="adjustment_detail",
    ),
    path(
        "adjustments/<int:pk>/post/",
        adjustment_views.SalesAdjustmentTransitionView.as_view(action="post"),
        name="adjustment_post",
    ),
    path(
        "adjustments/<int:pk>/reverse/",
        adjustment_views.SalesAdjustmentTransitionView.as_view(action="reverse"),
        name="adjustment_reverse",
    ),
    path(
        "adjustment-lines/<int:pk>/delete/",
        adjustment_views.SalesAdjustmentLineDeleteView.as_view(),
        name="adjustment_line_delete",
    ),
]
