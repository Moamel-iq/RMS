"""
Procurement reads. Nothing here writes.

Every selector starts from the caller, not from an identifier. A function that
took an id and returned an object would have to be checked by whoever called
it, and the first caller to forget is the leak — so out-of-scope objects are
simply not in the queryset.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.inventory.selectors import reachable_organization_ids
from apps.organizations.authorization import OutOfScope
from apps.procurement.models import Supplier
from apps.users.models import User


def visible_suppliers(user: User) -> QuerySet[Supplier]:
    """
    Every supplier in an organization the caller reaches — **archived ones
    included**.

    Archived suppliers stay visible on purpose. Their code is still reserved,
    every posted document still points at them, and a screen that hid them
    would make a taken code look free and leave no way to reactivate one
    archived by mistake.

    Reuses `reachable_organization_ids` rather than restating it. Reaching an
    organization means holding organization authority over it or a membership
    at any of its branches, and a second definition here would eventually
    disagree with the first.
    """
    return Supplier.objects.filter(
        organization_id__in=reachable_organization_ids(user)
    ).select_related("organization")


def resolve_supplier(user: User, supplier_id: int) -> Supplier:
    """
    Turn a submitted supplier id into one the caller may reach.

    Resolved **with** the caller, never fetched and then checked: there is no
    moment where an out-of-scope supplier exists in a local variable. Out of
    scope answers 404 with the same wording a missing row gets, because a 403
    would confirm the supplier exists and ids are sequential.
    """
    supplier = visible_suppliers(user).filter(pk=supplier_id).first()
    if supplier is None:
        raise OutOfScope(_("Supplier %(id)s does not exist.") % {"id": supplier_id})
    return supplier
