"""
Warehouses, warehouse scope, and the inventory permission map.

Warehouse scope is the one thing Phase 0's authorization layer did not have,
and it is the piece most likely to be got subtly wrong: it must *narrow* what
a branch membership granted and must never widen it. Every test here names a
concrete warehouse.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.inventory.models import Warehouse, WarehouseType
from apps.inventory.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_SCOPE,
    VIEW_VALUATION,
    PermissionScope,
    permissions_for_role,
    scope_of,
)
from apps.inventory.selectors import visible_warehouses
from apps.inventory.services import (
    create_warehouse,
    ensure_in_transit_warehouse,
    update_warehouse,
)
from apps.organizations.authorization import (
    OutOfScope,
    PermissionMissing,
    can_access_warehouse,
    require_warehouse_permission,
    resolve_warehouse,
)
from apps.organizations.models import (
    Branch,
    BranchMembership,
    Role,
    WarehouseScopeMode,
)
from apps.organizations.services import set_membership_warehouse_scope
from apps.users.models import User

pytestmark = pytest.mark.django_db


class TestWarehouseModel:
    def test_code_is_unique_per_branch(self, branch: Branch, main_store: Warehouse) -> None:
        with pytest.raises(ValidationError):
            create_warehouse(branch=branch, code="MAIN", name="مكرر")

    def test_the_same_code_is_allowed_at_another_branch(
        self, second_branch: Branch, main_store: Warehouse
    ) -> None:
        twin = create_warehouse(branch=second_branch, code="MAIN", name="المخزن الرئيسي")
        assert twin.pk != main_store.pk

    def test_the_code_is_canonicalised(self, branch: Branch) -> None:
        warehouse = create_warehouse(branch=branch, code=" dry-store ", name="مخزن جاف")
        assert warehouse.code == "DRY-STORE"

    def test_in_transit_cannot_be_created_by_hand(self, branch: Branch) -> None:
        with pytest.raises(ValidationError) as caught:
            create_warehouse(
                branch=branch,
                code="TRANSIT",
                name="بالطريق",
                warehouse_type=WarehouseType.IN_TRANSIT,
            )
        assert caught.value.code == "system_warehouse_not_user_creatable"

    def test_the_system_in_transit_warehouse_is_created_once(self, branch: Branch) -> None:
        first = ensure_in_transit_warehouse(branch=branch)
        second = ensure_in_transit_warehouse(branch=branch)

        assert first.pk == second.pk
        assert first.is_system is True
        assert first.warehouse_type == WarehouseType.IN_TRANSIT

    def test_a_system_warehouse_cannot_be_renamed_or_archived(self, branch: Branch) -> None:
        in_transit = ensure_in_transit_warehouse(branch=branch)

        with pytest.raises(ValidationError) as caught:
            update_warehouse(warehouse=in_transit, name="شيء آخر")
        assert caught.value.code == "system_warehouse_protected"

        with pytest.raises(ValidationError):
            update_warehouse(warehouse=in_transit, name=in_transit.name, is_active=False)

    def test_the_database_refuses_a_second_in_transit_per_branch(self, branch: Branch) -> None:
        ensure_in_transit_warehouse(branch=branch)

        with pytest.raises(IntegrityError), transaction.atomic():
            Warehouse.objects.create(
                branch=branch,
                code="TRANSIT2",
                name="ثاني",
                warehouse_type=WarehouseType.IN_TRANSIT,
                is_system=True,
            )

    def test_the_database_refuses_a_system_flag_on_an_ordinary_warehouse(
        self, main_store: Warehouse
    ) -> None:
        """`is_system` must never be a way to exempt an ordinary store."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Warehouse.objects.filter(pk=main_store.pk).update(is_system=True)


class TestWarehouseScope:
    def test_all_mode_reaches_every_warehouse_in_the_branch(
        self, manager: User, main_store: Warehouse, kitchen_store: Warehouse
    ) -> None:
        reachable = set(visible_warehouses(manager).values_list("pk", flat=True))
        assert reachable == {main_store.pk, kitchen_store.pk}

    def test_all_mode_includes_a_warehouse_created_later(
        self, manager: User, branch: Branch, main_store: Warehouse
    ) -> None:
        """
        This is why `ALL` is a mode and not an expanded list of rows: a
        membership granted "all warehouses" must cover the one that opens next
        month.
        """
        assert can_access_warehouse(manager, main_store)

        newly_opened = create_warehouse(branch=branch, code="COLD", name="مخزن مبرد")

        assert can_access_warehouse(User.objects.get(pk=manager.pk), newly_opened)

    def test_selected_mode_restricts_to_the_listed_warehouses(
        self, manager: User, branch: Branch, main_store: Warehouse, kitchen_store: Warehouse
    ) -> None:
        membership = BranchMembership.objects.get(user=manager, branch=branch)
        set_membership_warehouse_scope(
            membership=membership,
            mode=WarehouseScopeMode.SELECTED,
            warehouses=[main_store],
        )

        fresh = User.objects.get(pk=manager.pk)
        assert can_access_warehouse(fresh, main_store)
        assert not can_access_warehouse(fresh, kitchen_store)

    def test_selected_mode_does_not_include_warehouses_created_later(
        self, manager: User, branch: Branch, main_store: Warehouse
    ) -> None:
        membership = BranchMembership.objects.get(user=manager, branch=branch)
        set_membership_warehouse_scope(
            membership=membership,
            mode=WarehouseScopeMode.SELECTED,
            warehouses=[main_store],
        )
        newly_opened = create_warehouse(branch=branch, code="COLD", name="مخزن مبرد")

        assert not can_access_warehouse(User.objects.get(pk=manager.pk), newly_opened)

    def test_a_selection_cannot_cross_branches(
        self, manager: User, branch: Branch, other_warehouse: Warehouse
    ) -> None:
        """
        Warehouse scope narrows branch access. Allowing a selection from
        another branch would let it *widen* access, which is the exact
        inversion the model exists to prevent.
        """
        membership = BranchMembership.objects.get(user=manager, branch=branch)

        with pytest.raises(ValidationError) as caught:
            set_membership_warehouse_scope(
                membership=membership,
                mode=WarehouseScopeMode.SELECTED,
                warehouses=[other_warehouse],
            )
        assert caught.value.code == "warehouse_branch_mismatch"

    def test_selected_mode_needs_at_least_one_warehouse(
        self, manager: User, branch: Branch
    ) -> None:
        membership = BranchMembership.objects.get(user=manager, branch=branch)

        with pytest.raises(ValidationError) as caught:
            set_membership_warehouse_scope(
                membership=membership, mode=WarehouseScopeMode.SELECTED, warehouses=[]
            )
        assert caught.value.code == "no_warehouse_selected"

    def test_existing_memberships_default_to_all(self, manager: User, branch: Branch) -> None:
        """The migration must not silently revoke anybody's access."""
        membership = BranchMembership.objects.get(user=manager, branch=branch)
        assert membership.warehouse_scope_mode == WarehouseScopeMode.ALL

    def test_organization_authority_reaches_every_warehouse(
        self, accounting_manager: User, main_store: Warehouse, kitchen_store: Warehouse
    ) -> None:
        assert can_access_warehouse(accounting_manager, main_store)
        assert can_access_warehouse(accounting_manager, kitchen_store)

    def test_a_superuser_reaches_everything(
        self, superuser: User, main_store: Warehouse, other_warehouse: Warehouse
    ) -> None:
        assert can_access_warehouse(superuser, main_store)
        assert can_access_warehouse(superuser, other_warehouse)


class TestCrossTenantWarehouseAccess:
    def test_a_foreign_branch_warehouse_is_a_404(
        self, rival_manager: User, main_store: Warehouse
    ) -> None:
        with pytest.raises(OutOfScope):
            resolve_warehouse(rival_manager, main_store.pk)

    def test_a_foreign_warehouse_is_invisible_in_the_list(
        self, rival_manager: User, main_store: Warehouse, other_warehouse: Warehouse
    ) -> None:
        reachable = set(visible_warehouses(rival_manager).values_list("pk", flat=True))
        assert reachable == {other_warehouse.pk}

    def test_out_of_scope_is_404_and_missing_permission_is_403(
        self, storekeeper: User, main_store: Warehouse, other_warehouse: Warehouse
    ) -> None:
        """
        The distinction ADR-016's amendment turns on. A warehouse they cannot
        reach does not exist for them; one they can reach but may not act on
        is an honest refusal.
        """
        with pytest.raises(OutOfScope):
            require_warehouse_permission(storekeeper, "inventory.post_receipt", other_warehouse)

        with pytest.raises(PermissionMissing):
            require_warehouse_permission(storekeeper, "inventory.post_waste", main_store)


class TestPermissionMap:
    def test_every_permission_is_migrated(self) -> None:
        from django.contrib.auth.models import Permission

        codenames = set(
            Permission.objects.filter(content_type__app_label="inventory").values_list(
                "codename", flat=True
            )
        )
        for permission in ALL_PERMISSIONS:
            assert permission.split(".", 1)[1] in codenames, permission

    def test_every_permission_declares_a_scope(self) -> None:
        assert set(PERMISSION_SCOPE) == set(ALL_PERMISSIONS)

    def test_posting_permissions_are_warehouse_scoped(self) -> None:
        for permission in (
            "inventory.post_receipt",
            "inventory.post_issue",
            "inventory.post_transfer",
            "inventory.post_waste",
            "inventory.conduct_stock_count",
        ):
            assert scope_of(permission) is PermissionScope.WAREHOUSE

    def test_master_data_permissions_need_organization_reach(self) -> None:
        """
        Reach, not organization membership. A branch manager maintains the
        shared item master; requiring organization-wide authority for that
        would hand out period-closing power as a side effect.
        """
        for permission in (
            "inventory.manage_items",
            "inventory.manage_categories",
            "inventory.manage_conversions",
            "inventory.manage_package_units",
        ):
            assert scope_of(permission) is PermissionScope.ORGANIZATION_MASTER_DATA

    def test_elevated_acts_need_real_organization_authority(self) -> None:
        for permission in (
            "inventory.post_opening_stock",
            "inventory.override_negative_stock",
        ):
            assert scope_of(permission) is PermissionScope.ORGANIZATION_AUTHORITY

    def test_an_unknown_permission_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            scope_of("inventory.invent_stock")

    def test_a_storekeeper_never_sees_cost(self) -> None:
        granted = permissions_for_role(Role.STOREKEEPER)
        assert VIEW_VALUATION not in granted

    def test_a_storekeeper_cannot_waste_adjust_reverse_or_override(self) -> None:
        granted = permissions_for_role(Role.STOREKEEPER)
        for forbidden in (
            "inventory.post_waste",
            "inventory.post_adjustment",
            "inventory.reverse_movement",
            "inventory.override_negative_stock",
            "inventory.approve_stock_count",
        ):
            assert forbidden not in granted

    def test_purchasing_gets_no_master_data_mutation_or_receipt_posting(self) -> None:
        granted = permissions_for_role(Role.PURCHASING)
        assert granted == frozenset(
            {"inventory.view_item", "inventory.view_stock", "inventory.view_valuation"}
        )

    def test_a_normal_accountant_cannot_post_opening_stock(self) -> None:
        assert "inventory.post_opening_stock" not in permissions_for_role(Role.ACCOUNTANT)
        assert "inventory.post_opening_stock" in permissions_for_role(Role.ACCOUNTING_MANAGER)

    def test_the_accounting_manager_performs_no_warehouse_operations(self) -> None:
        granted = permissions_for_role(Role.ACCOUNTING_MANAGER)
        for operational in (
            "inventory.post_receipt",
            "inventory.post_issue",
            "inventory.post_transfer",
            "inventory.conduct_stock_count",
        ):
            assert operational not in granted

    def test_a_viewer_sees_no_valuation(self) -> None:
        assert VIEW_VALUATION not in permissions_for_role(Role.VIEWER)

    def test_a_cashier_holds_nothing(self) -> None:
        assert permissions_for_role(Role.CASHIER) == frozenset()

    def test_the_manager_holds_both_count_permissions(self) -> None:
        """
        Deliberate: maker-checker is enforced on the act
        (`approver_id != conductor_id`) in Task 1.6, not by withholding the
        permission. A small branch needs one person able to do both jobs on
        different counts.
        """
        granted = permissions_for_role(Role.MANAGER)
        assert "inventory.conduct_stock_count" in granted
        assert "inventory.approve_stock_count" in granted

    def test_every_role_is_mapped(self) -> None:
        from apps.inventory.permissions import ROLE_PERMISSIONS

        assert set(ROLE_PERMISSIONS) == {role.value for role in Role}

    def test_groups_carry_the_permissions_after_a_grant(self, storekeeper: User) -> None:
        assert storekeeper.has_perm("inventory.post_receipt")
        assert not storekeeper.has_perm("inventory.view_valuation")
