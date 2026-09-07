"""Create or repair the initial production owner without storing a password in Git."""

from __future__ import annotations

import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.accounting.permissions import sync_role_groups as sync_accounting_role_groups
from apps.core.permissions import sync_role_groups as sync_core_role_groups
from apps.hr.permissions import sync_role_groups as sync_hr_role_groups
from apps.inventory.permissions import sync_role_groups as sync_inventory_role_groups
from apps.kitchen.permissions import sync_role_groups as sync_kitchen_role_groups
from apps.organizations.models import Organization, Role
from apps.organizations.permissions import sync_user_role_groups
from apps.organizations.security_permissions import sync_role_groups as sync_security_role_groups
from apps.organizations.services import grant_organization_access
from apps.procurement.permissions import sync_role_groups as sync_procurement_role_groups
from apps.sales.permissions import sync_role_groups as sync_sales_role_groups
from apps.supplier_quotes.permissions import sync_role_groups as sync_supplier_quote_role_groups
from apps.users.models import User


def _sync_builtin_role_groups() -> None:
    """Rebuild every built-in role group after a production migration."""
    for sync in (
        sync_core_role_groups,
        sync_security_role_groups,
        sync_inventory_role_groups,
        sync_procurement_role_groups,
        sync_kitchen_role_groups,
        sync_sales_role_groups,
        sync_accounting_role_groups,
        sync_hr_role_groups,
        sync_supplier_quote_role_groups,
    ):
        sync()


class Command(BaseCommand):
    help = (
        "Synchronize built-in role groups and make the configured production owner "
        "an active owner of one organization."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--organization-code",
            default=os.environ.get("RENDER_OWNER_ORGANIZATION_CODE", "01"),
            help="Organization code for the initial owner (default: 01).",
        )
        parser.add_argument(
            "--owner-username",
            default=os.environ.get("RENDER_OWNER_USERNAME", "moamel"),
            help="Existing owner username, or the username to create from RENDER_OWNER_PASSWORD.",
        )
        parser.add_argument(
            "--owner-password",
            default=None,
            help="One-time password used only when the owner account does not exist.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        organization_code = str(options["organization_code"]).strip().upper()
        owner_username = str(options["owner_username"]).strip()
        owner_password = options["owner_password"] or os.environ.get("RENDER_OWNER_PASSWORD")

        if not organization_code:
            raise CommandError("An organization code is required.")
        if not owner_username:
            raise CommandError("An owner username is required.")

        with transaction.atomic():
            _sync_builtin_role_groups()
            organization = Organization.objects.filter(
                code=organization_code, is_active=True
            ).first()
            if organization is None:
                raise CommandError(f"Active organization {organization_code!r} was not found.")

            owner = User.objects.filter(username=owner_username).first()
            created = owner is None
            if owner is None:
                if not owner_password:
                    raise CommandError(
                        "The owner account does not exist. Set RENDER_OWNER_PASSWORD as a Render secret "
                        "or pass --owner-password for this one-time bootstrap."
                    )
                owner = User.objects.create_superuser(
                    username=owner_username, password=owner_password
                )
            else:
                changes: list[str] = []
                if not owner.is_active:
                    owner.is_active = True
                    changes.append("is_active")
                if not owner.is_staff:
                    owner.is_staff = True
                    changes.append("is_staff")
                if not owner.is_superuser:
                    owner.is_superuser = True
                    changes.append("is_superuser")
                if changes:
                    owner.save(update_fields=changes)

            grant_organization_access(user=owner, organization=organization, role=Role.OWNER)
            sync_user_role_groups(owner)

        state = "created" if created else "repaired"
        self.stdout.write(
            self.style.SUCCESS(
                f"Production access {state}: {owner.username} is the active owner of {organization.code}."
            )
        )
