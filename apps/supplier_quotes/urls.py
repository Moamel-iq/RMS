from django.urls import path

from apps.supplier_quotes import views

app_name = "supplier_quotes"
urlpatterns = [
    path("", views.QuoteListView.as_view(), name="list"),
    path("new/", views.QuoteCreateView.as_view(), name="create"),
    path("<int:pk>/", views.QuoteDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.QuoteEditView.as_view(), name="edit"),
    path("<int:pk>/delete/", views.QuoteDeleteView.as_view(), name="delete"),
    path("<int:pk>/lines/<int:line_pk>/edit/", views.QuoteLineEditView.as_view(), name="line_edit"),
    path(
        "<int:pk>/lines/<int:line_pk>/delete/",
        views.QuoteLineDeleteView.as_view(),
        name="line_delete",
    ),
    path(
        "<int:pk>/attachments/upload/",
        views.QuoteAttachmentUploadView.as_view(),
        name="attachment_upload",
    ),
    path(
        "<int:pk>/attachments/<int:attachment_pk>/delete/",
        views.QuoteAttachmentDeleteView.as_view(),
        name="attachment_delete",
    ),
    path(
        "<int:pk>/attachments/<int:attachment_pk>/download/",
        views.QuoteAttachmentDownloadView.as_view(),
        name="attachment_download",
    ),
]
