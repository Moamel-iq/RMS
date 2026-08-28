"""
Stock locations under a real COMMIT boundary.

Split out of `test_locations.py` so that file can share one demo seed
across its module. A transactional test needs a genuine COMMIT and
truncates the tables afterwards, neither of which is possible inside the
outer block a shared seed holds open — it would pass while proving
nothing, which for a concurrency test is the worst available outcome.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction

from apps.core.context import audit_context
from apps.inventory import locations
from apps.inventory.models import (
    InventoryItem,
    StockLocation,
    Warehouse,
)
from apps.inventory.reconciliation import verify_locations
from apps.organizations.models import Organization
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.mark.django_db(transaction=True)
class TestLocationConcurrency:
    def test_two_put_aways_cannot_both_take_the_same_unlocated_stock(
        self, settings: object
    ) -> None:
        """
        Real COMMIT boundary.

        Both transactions read the same unlocated remainder; the advisory lock
        is on `(warehouse, item, lot)` rather than the bin, so the second waits
        and then sees what the first took. A per-bin lock would let both
        succeed and the bins would together claim more than the warehouse has.
        """
        import threading

        settings.DEBUG = True  # type: ignore[attr-defined]
        call_command("seed_units", verbosity=0)
        user = User.objects.create_user(username="race", password="pw-not-real-1234")
        call_command(
            "seed_inventory_demo", user=user.username, confirm_demo=True, stdout=StringIO()
        )

        organization = Organization.objects.get(code="DEMO-KHAN-MANDI")
        warehouse = Warehouse.objects.get(branch__organization=organization, code="DEMO-KITCHEN")
        item = InventoryItem.objects.get(organization=organization, code="DEMO-RICE")
        held = locations.warehouse_quantity(warehouse, item, None)

        with audit_context(actor=user):
            bin_a = locations.create_location(warehouse=warehouse, code="RACE-A", name="أ")
            bin_b = locations.create_location(warehouse=warehouse, code="RACE-B", name="ب")

        results: list[str] = []

        def put_away(location: StockLocation) -> None:
            from django.db import connection as thread_connection

            try:
                with transaction.atomic(), audit_context(actor=user):
                    locations.put_away(location=location, item=item, quantity=held)
                results.append("ok")
            except ValidationError as refusal:
                results.append(str(refusal.code))
            finally:
                thread_connection.close()

        threads = [
            threading.Thread(target=put_away, args=(bin_a,)),
            threading.Thread(target=put_away, args=(bin_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results) == ["location_put_away_exceeds_unlocated", "ok"]
        assert locations.located_total(warehouse, item, None) == held
        assert verify_locations(organization) == []
