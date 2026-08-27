from django.urls import path

from apps.hr import (
    attendance_views,
    operation_views,
    payroll_views,
    shift_views,
    statement_views,
    views,
)
from apps.hr.permissions import VIEW_ADVANCE, VIEW_DEDUCTION, VIEW_LEAVE, VIEW_OVERTIME

app_name = "hr"

urlpatterns = [
    # Mounted at the module root, like the other modules: the summary first.
    path("", views.HrOverviewView.as_view(), name="overview"),
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
        "employees/<int:pk>/documents/<int:document_pk>/download/",
        views.EmployeeDocumentDownloadView.as_view(),
        name="employee_document_download",
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
    path("leaves/", operation_views.LeaveListView.as_view(), name="leave_list"),
    path("leaves/new/", operation_views.LeaveCreateView.as_view(), name="leave_create"),
    path(
        "leaves/approvals/", operation_views.LeaveApprovalListView.as_view(), name="leave_approvals"
    ),
    path("leaves/calendar/", operation_views.LeaveCalendarView.as_view(), name="leave_calendar"),
    path("leaves/types/", operation_views.LeaveTypeWorkspace.as_view(), name="leave_types"),
    path("leaves/<int:pk>/", operation_views.LeaveDetailView.as_view(), name="leave_detail"),
    path(
        "leaves/<int:pk>/<str:action>/",
        operation_views.OperationCommandView.as_view(kind="leave", required_permission=VIEW_LEAVE),
        name="leave_command",
    ),
    path("absences/", operation_views.AbsenceWorkspace.as_view(), name="absence_list"),
    path(
        "absences/<int:pk>/<str:business_date>/classify/",
        operation_views.AbsenceClassifyView.as_view(),
        name="absence_classify",
    ),
    path("overtime/", operation_views.OvertimeListView.as_view(), name="overtime_list"),
    path("overtime/new/", operation_views.OvertimeCreateView.as_view(), name="overtime_create"),
    path(
        "overtime/<int:pk>/", operation_views.OvertimeDetailView.as_view(), name="overtime_detail"
    ),
    path(
        "overtime/<int:pk>/<str:action>/",
        operation_views.OperationCommandView.as_view(
            kind="overtime", required_permission=VIEW_OVERTIME
        ),
        name="overtime_command",
    ),
    path("deductions/", operation_views.DeductionListView.as_view(), name="deduction_list"),
    path("deductions/new/", operation_views.DeductionCreateView.as_view(), name="deduction_create"),
    path(
        "deductions/<int:pk>/",
        operation_views.DeductionDetailView.as_view(),
        name="deduction_detail",
    ),
    path(
        "deductions/<int:pk>/<str:action>/",
        operation_views.OperationCommandView.as_view(
            kind="deduction", required_permission=VIEW_DEDUCTION
        ),
        name="deduction_command",
    ),
    path("advances/", operation_views.AdvanceListView.as_view(), name="advance_list"),
    path("advances/new/", operation_views.AdvanceCreateView.as_view(), name="advance_create"),
    path("advances/<int:pk>/", operation_views.AdvanceDetailView.as_view(), name="advance_detail"),
    path(
        "advances/<int:pk>/<str:action>/",
        operation_views.OperationCommandView.as_view(
            kind="advance", required_permission=VIEW_ADVANCE
        ),
        name="advance_command",
    ),
    path("payroll/", payroll_views.PayrollRunListView.as_view(), name="payroll_list"),
    path("payroll/new/", payroll_views.PayrollRunCreateView.as_view(), name="payroll_create"),
    path(
        "payroll/approvals/",
        payroll_views.PayrollApprovalListView.as_view(),
        name="payroll_approvals",
    ),
    path(
        "payroll/payments/",
        payroll_views.PayrollPaymentListView.as_view(),
        name="payroll_payments",
    ),
    path(
        "payroll/payments/<int:pk>/reverse/",
        payroll_views.PayrollPaymentReverseView.as_view(),
        name="payroll_payment_reverse",
    ),
    path(
        "payroll/<int:pk>/pay/",
        payroll_views.PayrollPaymentCreateView.as_view(),
        name="payroll_payment_create",
    ),
    path(
        "payroll/<int:pk>/",
        payroll_views.PayrollRunDetailView.as_view(),
        name="payroll_detail",
    ),
    path(
        "payroll/<int:pk>/<str:action>/",
        payroll_views.PayrollRunCommandView.as_view(),
        name="payroll_command",
    ),
    path(
        "payroll/lines/<int:pk>/",
        payroll_views.PayrollEmployeeLineView.as_view(),
        name="payroll_line",
    ),
    path(
        "statements/",
        statement_views.EmployeeStatementListView.as_view(),
        name="statement_list",
    ),
    path(
        "statements/employees/<int:pk>/",
        statement_views.EmployeeStatementDetailView.as_view(),
        name="employee_statement",
    ),
    path(
        "statements/payslips/<int:pk>/",
        statement_views.PayslipView.as_view(),
        name="payslip",
    ),
]
