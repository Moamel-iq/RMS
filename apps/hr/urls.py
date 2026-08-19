from django.urls import path

from apps.hr import attendance_views, shift_views, views

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
    path("shifts/", shift_views.ShiftListView.as_view(), name="shift_list"),
    path("shifts/new/", shift_views.ShiftCreateView.as_view(), name="shift_create"),
    path("shifts/<int:pk>/edit/", shift_views.ShiftUpdateView.as_view(), name="shift_update"),
    path(
        "shifts/assignments/",
        shift_views.ShiftAssignmentWorkspace.as_view(),
        name="shift_assignments",
    ),
    path(
        "shifts/schedules/employees/",
        shift_views.ScheduleView.as_view(mode="employee"),
        name="employee_schedule",
    ),
    path(
        "shifts/schedules/branches/",
        shift_views.ScheduleView.as_view(mode="branch"),
        name="branch_schedule",
    ),
    path(
        "shifts/rotations/",
        shift_views.ScheduleView.as_view(mode="rotation"),
        name="shift_rotations",
    ),
    path("attendance/", attendance_views.AttendanceDayListView.as_view(), name="attendance_list"),
    path(
        "attendance/new/",
        attendance_views.AttendanceEventCreateView.as_view(),
        name="attendance_create",
    ),
    path(
        "attendance/employees/<int:pk>/",
        attendance_views.EmployeeAttendanceView.as_view(),
        name="attendance_employee",
    ),
    path(
        "attendance/events/<int:pk>/correct/",
        attendance_views.AttendanceCorrectionView.as_view(),
        name="attendance_correct",
    ),
    path(
        "attendance/missing-punches/",
        attendance_views.MissingPunchView.as_view(),
        name="attendance_missing",
    ),
    path(
        "attendance/employees/<int:pk>/<str:business_date>/approve/",
        attendance_views.AttendanceApproveView.as_view(),
        name="attendance_approve",
    ),
    path(
        "attendance/employees/<int:pk>/<str:business_date>/reopen/",
        attendance_views.AttendanceReopenView.as_view(),
        name="attendance_reopen",
    ),
]
