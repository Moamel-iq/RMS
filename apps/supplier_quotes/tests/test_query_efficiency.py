"""Query-count contracts for supplier quote list and detail screens."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.shortcuts import get_object_or_404
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.inventory.models import (
    InventoryItem,
    ItemCategory,
    ItemType,
    PackageUnit,
)
from apps.organizations.models import Organization
from apps.supplier_quotes.models import (
    SupplierQuote,
    SupplierQuoteAttachment,
    SupplierQuoteLine,
)
from apps.supplier_quotes.views import QuoteDetailView, QuoteListView
from apps.units.models import Dimension, UnitOfMeasure
from apps.users.models import User


@dataclass(frozen=True)
class QuoteWorld:
    organization: Organization
    user: User
    item_quote: SupplierQuote


@pytest.fixture
def quote_world() -> QuoteWorld:
    organization = Organization.objects.create(code="SQPERF", name="اختبار العروض")
    user = User.objects.create_user(username="supplier-quote-query-user")
    unit = UnitOfMeasure.objects.create(
        code="SQPC",
        name="قطعة اختبار",
        dimension=Dimension.COUNT,
        factor_to_base=Decimal("1"),
        is_base=True,
    )
    category = ItemCategory.objects.create(
        organization=organization,
        code="SQFOOD",
        name="أصناف اختبار",
        depth=1,
    )
    package = PackageUnit.objects.create(
        organization=organization,
        code="SQBOX",
        name="صندوق اختبار",
    )
    items = [
        InventoryItem.objects.create(
            organization=organization,
            code=f"SQITEM{position}",
            name=f"صنف اختبار {position}",
            category=category,
            item_type=ItemType.RAW_MATERIAL,
            base_unit=unit,
        )
        for position in (1, 2)
    ]
    item_quote = SupplierQuote.objects.create(
        organization=organization,
        supplier_name="مورد الأصناف",
        created_by=user,
    )
    for sequence, item in enumerate(items, start=1):
        SupplierQuoteLine.objects.create(
            quote=item_quote,
            sequence=sequence,
            item=item,
            unit=package,
            quantity=Decimal("2"),
            unit_price=Decimal("1250"),
        )
    SupplierQuoteAttachment.objects.create(
        quote=item_quote,
        file="supplier-quotes/item-quote.pdf",
        original_name="item-quote.pdf",
        uploaded_by=user,
    )
    document_quote = SupplierQuote.objects.create(
        organization=organization,
        supplier_name="مورد المستند",
        created_by=user,
    )
    SupplierQuoteAttachment.objects.create(
        quote=document_quote,
        file="supplier-quotes/document-only.pdf",
        original_name="document-only.pdf",
        uploaded_by=user,
    )
    return QuoteWorld(
        organization=organization,
        user=user,
        item_quote=item_quote,
    )


def _request(world: QuoteWorld) -> Any:
    request = RequestFactory().get("/supplier-quotes/")
    request.user = world.user
    return request


def _touch_list_relations(quotes: list[SupplierQuote]) -> None:
    for quote in quotes:
        _ = quote.status_label
        _ = quote.total_amount


def _touch_detail_relations(quote: SupplierQuote) -> None:
    _ = quote.status_label
    _ = quote.total_amount
    list(quote.attachments.all())
    lines = list(quote.lines.all())
    for line in lines:
        str(line.item)
        if line.unit is not None:
            str(line.unit)
    _ = quote.total_amount


@pytest.mark.django_db
def test_quote_list_prefetch_reduces_six_queries_to_three(
    quote_world: QuoteWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_rows = SupplierQuote.objects.filter(organization=quote_world.organization)
    with CaptureQueriesContext(connection) as before:
        _touch_list_relations(list(base_rows))

    monkeypatch.setattr(
        "apps.supplier_quotes.views.organizations_with_permission",
        lambda _user, _permission: Organization.objects.filter(pk=quote_world.organization.pk),
    )
    with CaptureQueriesContext(connection) as after:
        rows = QuoteListView().get_queryset(_request(quote_world))
        _touch_list_relations(list(rows))

    assert len(before) == 6
    assert len(after) == 3


@pytest.mark.django_db
def test_quote_detail_prefetch_reduces_ten_queries_to_three(
    quote_world: QuoteWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CaptureQueriesContext(connection) as before:
        quote = get_object_or_404(SupplierQuote, pk=quote_world.item_quote.pk)
        _touch_detail_relations(quote)

    monkeypatch.setattr(
        "apps.supplier_quotes.views.organizations_with_permission",
        lambda _user, _permission: Organization.objects.filter(pk=quote_world.organization.pk),
    )
    with CaptureQueriesContext(connection) as after:
        quote = QuoteDetailView().detail_quote(_request(quote_world), quote_world.item_quote.pk)
        _touch_detail_relations(quote)

    assert len(before) == 10
    assert len(after) == 3
