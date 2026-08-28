from datetime import date

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from apps.organizations.authorization import organizations_with_permission
from apps.supplier_quotes.models import SupplierQuote
from apps.supplier_quotes.permissions import ADD, CHANGE, DELETE, VIEW
from apps.users.models import User
from apps.users.phone import normalize_iraqi_mobile

router = Router(tags=["supplier-quotes"])


def _actor(request: HttpRequest) -> User:
    """The API authentication layer has already refused anonymous callers."""
    user: User = request.user  # type: ignore[assignment]
    return user


class QuoteIn(Schema):
    organization_id: int
    supplier_name: str
    phone: str = ""
    quote_date: date | None = None
    notes: str = ""


def _serialize(row: SupplierQuote) -> dict[str, object]:
    return {
        "id": row.pk,
        "public_id": str(row.public_id),
        "organization_id": row.organization_id,
        "supplier_name": row.supplier_name,
        "phone": row.phone,
        "quote_date": row.quote_date.isoformat(),
        "status": row.status_label,
        "total_amount": format(row.total_amount, "f"),
    }


def _row(request: HttpRequest, quote_id: int, permission: str) -> SupplierQuote:
    actor = _actor(request)
    if not actor.has_perm(permission):
        raise PermissionDenied("لا تملك صلاحية هذا الإجراء.")
    try:
        return SupplierQuote.objects.get(
            organization__in=organizations_with_permission(actor, permission), pk=quote_id
        )
    except SupplierQuote.DoesNotExist as exc:
        raise ObjectDoesNotExist("عرض المورد غير موجود.") from exc


@router.get("/", response=list[dict[str, object]])
def list_quotes(
    request: HttpRequest, q: str = "", quote_date: date | None = None
) -> list[dict[str, object]]:
    actor = _actor(request)
    if not actor.has_perm(VIEW):
        raise PermissionDenied("لا تملك صلاحية عرض عروض الموردين.")
    rows = SupplierQuote.objects.filter(organization__in=organizations_with_permission(actor, VIEW))
    if q:
        rows = rows.filter(supplier_name__icontains=q) | rows.filter(phone__icontains=q)
    if quote_date:
        rows = rows.filter(quote_date=quote_date)
    return [_serialize(row) for row in rows]


@router.get("/{quote_id}", response=dict[str, object])
def get_quote(request: HttpRequest, quote_id: int) -> dict[str, object]:
    return _serialize(_row(request, quote_id, VIEW))


@router.post("/", response=dict[str, object])
def create_quote(request: HttpRequest, payload: QuoteIn) -> dict[str, object]:
    actor = _actor(request)
    if not actor.has_perm(ADD):
        raise PermissionDenied("لا تملك صلاحية إضافة عرض مورد.")
    organization = (
        organizations_with_permission(actor, ADD).filter(pk=payload.organization_id).first()
    )
    if organization is None:
        raise PermissionDenied("لا تملك صلاحية هذه المنظمة.")
    phone = normalize_iraqi_mobile(payload.phone.strip()) if payload.phone.strip() else ""
    row = SupplierQuote(
        organization=organization,
        supplier_name=payload.supplier_name.strip(),
        phone=phone,
        quote_date=payload.quote_date or timezone.localdate(),
        notes=payload.notes.strip(),
        created_by=actor,
    )
    row.full_clean()
    row.save()
    return _serialize(row)


@router.put("/{quote_id}", response=dict[str, object])
def update_quote(request: HttpRequest, quote_id: int, payload: QuoteIn) -> dict[str, object]:
    row = _row(request, quote_id, CHANGE)
    if payload.organization_id != row.organization_id:
        raise PermissionDenied("لا يمكن نقل عرض المورد بين المنظمات.")
    row.supplier_name, row.notes = payload.supplier_name.strip(), payload.notes.strip()
    row.phone = normalize_iraqi_mobile(payload.phone.strip()) if payload.phone.strip() else ""
    row.quote_date = payload.quote_date or row.quote_date
    row.full_clean()
    row.save()
    return _serialize(row)


@router.delete("/{quote_id}", response={204: None})
def delete_quote(request: HttpRequest, quote_id: int) -> tuple[int, None]:
    row = _row(request, quote_id, DELETE)
    for attachment in row.attachments.all():
        attachment.file.delete(save=False)
    row.delete()
    return 204, None
