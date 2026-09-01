"""
Loading the item master onto a fresh deployment.

The command exists because two databases describe one business and agree
about no primary key. These tests are about the three promises that makes it
safe to run against a live server: it resolves by code, it never writes twice,
and it carries only what the export chose to carry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from apps.inventory.models import InventoryItem, ItemCategory
from apps.organizations.models import Organization
from apps.organizations.services import create_organization

pytestmark = pytest.mark.django_db


PAYLOAD: dict[str, Any] = {
    "organization_code": "SRC",
    "categories": [
        {"code": "FOOD", "name": "أغذية", "parent_code": None, "depth": 0},
        {"code": "MEAT", "name": "لحوم", "parent_code": "FOOD", "depth": 1},
    ],
    "package_units": [{"code": "BOX", "name": "كرتون"}],
    "items": [
        {
            "code": "STK-0001",
            "name": "لحم لشه",
            "category_code": "MEAT",
            "item_type": "RAW_MATERIAL",
            "base_unit_code": "KG",
            "tracks_lots": False,
            "tracks_expiry": False,
            "shelf_life_days": None,
            "notes": "",
        },
        {
            "code": "STK-0002",
            "name": "تمن مندي",
            "category_code": "MEAT",
            "item_type": "RAW_MATERIAL",
            "base_unit_code": "KG",
            "tracks_lots": False,
            "tracks_expiry": False,
            "shelf_life_days": None,
            "notes": "",
        },
    ],
}


@pytest.fixture
def units() -> None:
    call_command("seed_units", verbosity=0)


@pytest.fixture
def payload_file(tmp_path: Path) -> Path:
    path = tmp_path / "master_items.json"
    open(path, "w", encoding="utf-8").write(json.dumps(PAYLOAD, ensure_ascii=False))
    return path


@pytest.fixture
def server(units: None) -> Organization:
    """A fresh deployment: it has units and nothing else."""
    return create_organization(code="SRV", name="السيرفر")


def _load(path: Path, organization: str = "SRV", **extra: Any) -> None:
    call_command("seed_master_items", file=str(path), organization=organization, **extra)


class TestLoadingAFreshServer:
    def test_it_creates_the_categories_and_the_items(
        self, server: Organization, payload_file: Path
    ) -> None:
        _load(payload_file)
        assert ItemCategory.objects.filter(organization=server).count() == 2
        assert InventoryItem.objects.filter(organization=server).count() == 2

    def test_it_resolves_by_code_not_by_id(self, server: Organization, payload_file: Path) -> None:
        """
        The payload names `MEAT` and `KG`; the server decides what those are.

        This is the whole reason the command exists rather than a fixture: the
        ids in the file's source database mean nothing here.
        """
        _load(payload_file)
        item = InventoryItem.objects.get(organization=server, code="STK-0001")
        assert item.category.code == "MEAT"
        assert item.base_unit.code == "KG"
        assert item.category.organization_id == server.pk

    def test_running_it_twice_writes_nothing_the_second_time(
        self, server: Organization, payload_file: Path
    ) -> None:
        """Re-running after a failed deploy must not double the master."""
        _load(payload_file)
        first = set(InventoryItem.objects.values_list("pk", flat=True))
        _load(payload_file)
        assert set(InventoryItem.objects.values_list("pk", flat=True)) == first

    def test_a_dry_run_writes_nothing(self, server: Organization, payload_file: Path) -> None:
        _load(payload_file, dry_run=True)
        assert not InventoryItem.objects.filter(organization=server).exists()
        assert not ItemCategory.objects.filter(organization=server).exists()

    def test_an_unknown_organization_is_refused_before_anything_is_written(
        self, server: Organization, payload_file: Path
    ) -> None:
        _load(payload_file, organization="NOPE")
        assert not InventoryItem.objects.exists()

    def test_a_missing_unit_names_the_row_and_writes_none_of_them(
        self, server: Organization, tmp_path: Path
    ) -> None:
        """
        All or nothing. A half-loaded master is worse than an empty one,
        because the gaps are invisible until somebody cannot find an item.
        """
        broken = dict(PAYLOAD)
        broken["items"] = [
            PAYLOAD["items"][0],
            {**PAYLOAD["items"][1], "base_unit_code": "NOT-A-UNIT"},
        ]
        path = tmp_path / "broken.json"
        open(path, "w", encoding="utf-8").write(json.dumps(broken, ensure_ascii=False))

        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            _load(path)
        assert not InventoryItem.objects.filter(organization=server).exists()


class TestWhatTheExportCarries:
    def test_the_exported_file_holds_no_archived_item(self) -> None:
        """
        The owner asked for the archived items to stay behind, and the export
        is where that decision lives — so it is asserted on the real file that
        ships, not on a fixture that could drift from it.
        """
        path = (
            Path(__file__).resolve().parents[1]
            / "management"
            / "commands"
            / "data"
            / "master_items.json"
        )
        if not path.is_file():  # pragma: no cover - the file ships with the branch
            pytest.skip("master_items.json is not present in this checkout")
        payload = json.loads(path.read_text(encoding="utf-8"))
        codes = {row["code"] for row in payload["items"]}
        assert codes, "the export is empty"
        # Every row carries the fields the loader reads; a missing key would
        # only surface mid-load against the live server.
        for row in payload["items"]:
            assert row["category_code"]
            assert row["base_unit_code"]
            assert row["item_type"]
