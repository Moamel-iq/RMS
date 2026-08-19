from django.urls import path

from apps.hr import views

app_name = "hr"

urlpatterns = [
    path("employees/", views.EmployeeListView.as_view(), name="employee_list"),
    path("employees/new/", views.EmployeeCreateView.as_view(), name="employee_create"),
    path("employees/<int:pk>/", views.EmployeeDetailView.as_view(), name="employee_detail"),
    path("employees/<int:pk>/edit/", views.EmployeeUpdateView.as_view(), name="employee_update"),
    path(
        "employees/<int:pk>/documents/",
        views.EmployeeDocumentCreateView.as_view(),
        name="employee_document_create",
    ),
    path(
        "employees/<int:pk>/archive/",
        views.EmployeeStatusView.as_view(action="archive"),
        name="employee_archive",
    ),
    path(
        "employees/<int:pk>/reactivate/",
        views.EmployeeStatusView.as_view(action="reactivate"),
        name="employee_reactivate",
    ),
    path(
        "employees/<int:pk>/terminate/",
        views.EmployeeStatusView.as_view(action="terminate"),
        name="employee_terminate",
    ),
    path("contracts/", views.ContractListView.as_view(), name="contract_list"),
    path("contracts/new/", views.ContractCreateView.as_view(), name="contract_create"),
    path("contracts/<int:pk>/", views.ContractDetailView.as_view(), name="contract_detail"),
    path("contracts/<int:pk>/edit/", views.ContractUpdateView.as_view(), name="contract_update"),
    path(
        "contracts/<int:pk>/approve/", views.ContractApproveView.as_view(), name="contract_approve"
    ),
]
