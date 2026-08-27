"""Arabic RTL Human Resources workspaces with HTMX fallbacks."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseRedirect,
)
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.core.models import AuditAction
from apps.core.selectors import audit_trail_for
from apps.core.services import record_audit_event
from apps.hr.dashboard import hr_overview
from apps.hr.forms import EmployeeContractForm, EmployeeDocumentForm, EmployeeForm
from apps.hr.models import (
    ContractStatus,
    Employee,
    EmployeeContract,
    EmployeeDocument,
    EmployeeStatus,
)
from apps.hr.permissions import (
    APPROVE_CONTRACT,
    MANAGE_CONTRACT,
    MANAGE_EMPLOYEE,
    TERMINATE_EMPLOYEE,
    VIEW_CONTRACT,
    VIEW_EMPLOYEE,
    VIEW_EMPLOYEE_PERSONAL,
    VIEW_EMPLOYEE_SALARY,
)
from apps.hr.selectors import (
    resolve_contract,
    resolve_employee,
    visible_contracts,
    visible_employees,
)
from apps.hr.services import (
    add_employee_document,
    approve_contract,
    archive_employee,
    create_contract,
    create_employee,
    parse_fixed_allowances,
    reactivate_employee,
    terminate_employee,
    update_contract,
    update_employee,
)
from apps.inventory.views import InventoryListView, InventoryViewMixin
from apps.organizations.authorization import (
    has_organization_permission,
    require_organization_permission,
)


class HumanResourcesMixin(InventoryViewMixin):
    module_key = "hr"


def _redirect(request: HttpRequest, url: str) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=200)
        response["HX-Redirect"] = url
        return response
    return HttpResponseRedirect(url)


class EmployeeListView(HumanResourcesMixin, InventoryListView):
    required_permission = VIEW_EMPLOYEE
    template_name = "hr/employee_list.html"
    context_object_name = "employees"
    page_title = _("الموظفون")
    page_hint = _(
        "الملف التشغيلي للموظف مع فرعه ووظيفته وحالته، من دون كشف البيانات الحساسة لغير المخوّلين."
    )
    search_fields = ("code", "name_ar", "name_en", "department", "job_title")
    manage_permission = MANAGE_EMPLOYEE
    create_url_name = "hr:employee_create"
    create_label = _("موظف جديد")
    result_label = _("موظف")

    def scoped_queryset(self) -> Any:
        queryset = visible_employees(self.actor).select_related("organization", "branch")
        status = self.request.GET.get("status", "").strip().upper()
        branch = self.request.GET.get("branch", "").strip()
        if status:
            queryset = queryset.filter(status=status)
        if branch.isdigit():
            queryset = queryset.filter(branch_id=int(branch))
        return queryset.order_by("code")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        visible = visible_employees(self.actor)
        context.update(
            {
                "statuses": EmployeeStatus.choices,
                "selected_status": self.request.GET.get("status", ""),
                "selected_branch": self.request.GET.get("branch", ""),
                "branches": {
                    employee.branch_id: employee.branch
                    for employee in visible.select_related("branch")
                }.values(),
            }
        )
        return context


class EmployeeWriteView(HumanResourcesMixin, View):
    required_permission = MANAGE_EMPLOYEE
    template_name = "hr/employee_form.html"
    instance: Employee | None = None

    def load(self) -> Employee | None:
        return None

    def page_title(self) -> Any:
        return _("تعديل الموظف") if self.instance else _("إضافة موظف")

    def build_form(self, data: Any = None) -> EmployeeForm:
        kwargs: dict[str, Any] = {"actor": self.actor, "instance": self.instance}
        if data is not None:
            kwargs["data"] = data
        return EmployeeForm(**kwargs)

    def context(self, form: EmployeeForm) -> dict[str, Any]:
        return {
            "form": form,
            "employee": self.instance,
            "page_title": self.page_title(),
            "page_hint": _("الرمز يُحجز دائماً داخل المؤسسة ولا يعاد استخدامه بعد الأرشفة."),
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.instance = self.load()
        return render(request, self.template_name, self.context(self.build_form()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.instance = self.load()
        form = self.build_form(request.POST)
        if form.is_valid():
            organization = form.cleaned_data["organization"]
            require_organization_permission(self.actor, MANAGE_EMPLOYEE, organization)
            values = form.cleaned_data.copy()
            values.pop("organization", None)
            values.pop("code", None)
            try:
                if self.instance is None:
                    employee = create_employee(
                        organization=organization,
                        code=form.cleaned_data["code"],
                        actor=self.actor,
                        **values,
                    )
                else:
                    employee = update_employee(employee=self.instance, **values)
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تم حفظ ملف الموظف."))
                return _redirect(request, reverse("hr:employee_detail", args=[employee.pk]))
        return render(request, self.template_name, self.context(form))


class EmployeeCreateView(EmployeeWriteView):
    pass


class EmployeeUpdateView(EmployeeWriteView):
    def load(self) -> Employee:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, MANAGE_EMPLOYEE, employee.organization)
        if employee.status == EmployeeStatus.ARCHIVED:
            raise Http404
        return employee


class EmployeeDetailView(HumanResourcesMixin, View):
    required_permission = VIEW_EMPLOYEE
    template_name = "hr/employee_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        organization = employee.organization
        may_personal = has_organization_permission(self.actor, VIEW_EMPLOYEE_PERSONAL, organization)
        may_salary = has_organization_permission(self.actor, VIEW_EMPLOYEE_SALARY, organization)
        may_contract = has_organization_permission(self.actor, VIEW_CONTRACT, organization)
        may_manage = has_organization_permission(self.actor, MANAGE_EMPLOYEE, organization)
        return render(
            request,
            self.template_name,
            {
                "employee": employee,
                "page_title": employee.display_name,
                "contracts": (
                    employee.contracts.select_related(
                        "branch", "payroll_policy", "created_by", "approved_by"
                    )
                    if may_contract
                    else EmployeeContract.objects.none()
                ),
                # A document normally contains identity or health/payroll
                # evidence.  It is not part of the general employee profile.
                "documents": (
                    employee.documents.select_related("created_by")
                    if may_personal
                    else EmployeeDocument.objects.none()
                ),
                "document_form": EmployeeDocumentForm() if may_manage and may_personal else None,
                "timeline": audit_trail_for(employee)[:50],
                "may_personal": may_personal,
                "may_salary": may_salary,
                "may_contract": may_contract,
                "may_manage": may_manage,
                "may_terminate": has_organization_permission(
                    self.actor, TERMINATE_EMPLOYEE, organization
                ),
                "may_manage_contract": has_organization_permission(
                    self.actor, MANAGE_CONTRACT, organization
                ),
            },
        )


class EmployeeDocumentCreateView(HumanResourcesMixin, View):
    required_permission = MANAGE_EMPLOYEE

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, MANAGE_EMPLOYEE, employee.organization)
        form = EmployeeDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                add_employee_document(employee=employee, actor=self.actor, **form.cleaned_data)
            except ValidationError as error:
                messages.error(request, "؛ ".join(error.messages))
            else:
                messages.success(request, _("أُضيف مستند الموظف."))
        else:
            messages.error(request, _("تعذر حفظ المستند؛ راجع الحقول."))
        return _redirect(request, reverse("hr:employee_detail", args=[employee.pk]))


class EmployeeDocumentDownloadView(HumanResourcesMixin, View):
    """Serve HR attachments only after the personal-data permission check."""

    required_permission = VIEW_EMPLOYEE

    # HttpResponseBase, not HttpResponse: an attachment streams, and
    # FileResponse descends from StreamingHttpResponse rather than from
    # HttpResponse. Django's dispatch contract is the base class either way.
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, VIEW_EMPLOYEE_PERSONAL, employee.organization)
        document = employee.documents.filter(pk=self.kwargs["document_pk"]).first()
        if document is None or not document.file:
            raise Http404
        # The guard above already settled this: a FieldFile is falsy exactly
        # when it has no stored name, so an unnamed file never reaches here.
        stored_name = document.file.name or ""
        record_audit_event(
            action=AuditAction.DOCUMENT_DOWNLOADED,
            target=document,
            branch=employee.branch,
            organization=employee.organization,
            reason="employee document downloaded",
            metadata={"employee_id": employee.pk, "document_id": document.pk},
        )
        return FileResponse(
            document.file.open("rb"),
            as_attachment=True,
            filename=stored_name.rsplit("/", maxsplit=1)[-1],
        )


class EmployeeDocumentRawMediaBlockView(View):
    """Prevent Django's DEBUG media helper from bypassing HR authorization."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        raise Http404


class EmployeeStatusView(HumanResourcesMixin, View):
    required_permission = MANAGE_EMPLOYEE
    action = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        employee = resolve_employee(self.actor, self.kwargs["pk"])
        reason = request.POST.get("reason", "").strip()
        permission = TERMINATE_EMPLOYEE if self.action == "terminate" else MANAGE_EMPLOYEE
        require_organization_permission(self.actor, permission, employee.organization)
        try:
            if self.action == "archive":
                archive_employee(employee=employee, reason=reason)
                message = _("أُرشف ملف الموظف مع بقاء تاريخه.")
            elif self.action == "reactivate":
                reactivate_employee(employee=employee, reason=reason)
                message = _("أُعيد تفعيل ملف الموظف.")
            elif self.action == "terminate":
                field = forms.DateField()
                date_value = field.clean(request.POST.get("termination_date"))
                terminate_employee(employee=employee, termination_date=date_value, reason=reason)
                message = _("أُنهيت خدمة الموظف وأُغلقت عقوده النشطة.")
            else:  # pragma: no cover - URL construction
                raise Http404
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, message)
        return _redirect(request, reverse("hr:employee_detail", args=[employee.pk]))


class ContractListView(HumanResourcesMixin, InventoryListView):
    required_permission = VIEW_CONTRACT
    template_name = "hr/contract_list.html"
    context_object_name = "contracts"
    page_title = _("العقود والأجور")
    page_hint = _(
        "كل تغيير راتب هو إصدار عقد جديد؛ العقد المعتمد يبقى دليلاً ثابتاً للفترات السابقة."
    )
    search_fields = ("employee__code", "employee__name_ar", "job_title", "department")
    manage_permission = MANAGE_CONTRACT
    create_url_name = "hr:contract_create"
    create_label = _("مسودة عقد")
    result_label = _("عقد")

    def scoped_queryset(self) -> Any:
        queryset = visible_contracts(self.actor).select_related(
            "employee", "organization", "branch", "payroll_policy", "approved_by"
        )
        status = self.request.GET.get("status", "").strip().upper()
        if status:
            queryset = queryset.filter(status=status)
        return queryset.order_by("employee__code", "-version")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statuses"] = ContractStatus.choices
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class ContractWriteView(HumanResourcesMixin, View):
    required_permission = MANAGE_CONTRACT
    template_name = "hr/contract_form.html"
    instance: EmployeeContract | None = None
    selected_employee: Employee | None = None

    def load(self) -> EmployeeContract | None:
        return None

    def employee_from_query(self) -> Employee | None:
        raw = self.request.GET.get("employee", "")
        return resolve_employee(self.actor, int(raw)) if raw.isdigit() else None

    def build_form(self, data: Any = None) -> EmployeeContractForm:
        kwargs: dict[str, Any] = {
            "actor": self.actor,
            "instance": self.instance,
            "employee": self.selected_employee,
        }
        if data is not None:
            kwargs["data"] = data
        return EmployeeContractForm(**kwargs)

    def context(self, form: EmployeeContractForm) -> dict[str, Any]:
        return {
            "form": form,
            "contract": self.instance,
            "page_title": _("تعديل مسودة العقد") if self.instance else _("إضافة مسودة عقد"),
            "page_hint": _("لا يصبح العقد مرجعاً للرواتب حتى يعتمده مستخدم آخر."),
            "form_base_template": "settings/_form_fragment.html"
            if self.is_htmx()
            else "shell.html",
        }

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.instance = self.load()
        self.selected_employee = (
            self.instance.employee if self.instance else self.employee_from_query()
        )
        return render(request, self.template_name, self.context(self.build_form()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.instance = self.load()
        form = self.build_form(request.POST)
        if form.is_valid():
            employee = form.cleaned_data["employee"]
            require_organization_permission(self.actor, MANAGE_CONTRACT, employee.organization)
            values = form.cleaned_data.copy()
            values.pop("employee")
            allowances_raw = values.pop("fixed_allowances_text", "")
            allowances = parse_fixed_allowances(allowances_raw)
            try:
                if self.instance is None:
                    contract = create_contract(
                        employee=employee,
                        actor=self.actor,
                        fixed_allowances=allowances,
                        **values,
                    )
                else:
                    contract = update_contract(
                        contract=self.instance,
                        fixed_allowances=allowances,
                        **values,
                    )
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
            else:
                messages.success(request, _("تم حفظ مسودة العقد."))
                return _redirect(request, reverse("hr:contract_detail", args=[contract.pk]))
        return render(request, self.template_name, self.context(form))


class ContractCreateView(ContractWriteView):
    pass


class ContractUpdateView(ContractWriteView):
    def load(self) -> EmployeeContract:
        contract = resolve_contract(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, MANAGE_CONTRACT, contract.organization)
        if contract.status != ContractStatus.DRAFT:
            raise Http404
        return contract


class ContractDetailView(HumanResourcesMixin, View):
    required_permission = VIEW_CONTRACT
    template_name = "hr/contract_detail.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        contract = resolve_contract(self.actor, self.kwargs["pk"])
        return render(
            request,
            self.template_name,
            {
                "contract": contract,
                "employee": contract.employee,
                "page_title": str(contract),
                "may_salary": has_organization_permission(
                    self.actor, VIEW_EMPLOYEE_SALARY, contract.organization
                ),
                "may_edit": contract.status == ContractStatus.DRAFT
                and has_organization_permission(self.actor, MANAGE_CONTRACT, contract.organization),
                "may_approve": contract.status == ContractStatus.DRAFT
                and contract.created_by_id != self.actor.pk
                and has_organization_permission(
                    self.actor, APPROVE_CONTRACT, contract.organization
                ),
                "timeline": audit_trail_for(contract)[:50],
            },
        )


class ContractApproveView(HumanResourcesMixin, View):
    required_permission = APPROVE_CONTRACT

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        contract = resolve_contract(self.actor, self.kwargs["pk"])
        require_organization_permission(self.actor, APPROVE_CONTRACT, contract.organization)
        try:
            approve_contract(contract=contract, actor=self.actor)
        except ValidationError as error:
            messages.error(request, "؛ ".join(error.messages))
        else:
            messages.success(request, _("اعتمد العقد وتجمّدت نسخة الأجر."))
        return _redirect(request, reverse("hr:contract_detail", args=[contract.pk]))


class HrOverviewView(HumanResourcesMixin, View):
    """
    The module's opening screen: headcount, contracts, and — with the salary
    permission — the monthly payroll as an aggregate.

    No individual salary renders here under any permission. The aggregate is
    the management fact; the person-level figure belongs to the employee
    screens that audit their own access.
    """

    required_permission = VIEW_EMPLOYEE
    template_name = "hr/overview.html"

    @property
    def include_salary(self) -> bool:
        return bool(self.request.user.has_perm(VIEW_EMPLOYEE_SALARY))

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        overview = hr_overview(self.actor, include_salary=self.include_salary)
        return render(
            request,
            self.template_name,
            {
                "overview": overview,
                "show_salary": self.include_salary,
                "page_title": _("نظرة عامة على الموارد البشرية"),
                "page_hint": _("الملاك الوظيفي والعقود، والرواتب مجاميع لا أفراداً."),
            },
        )
