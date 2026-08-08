"""
Write the accounting role-to-permission table into the Django groups.

Runs automatically after every migrate. Exposed as a command as well so it can
be run on demand — after restoring a database, or to check what a role
actually holds without reading the table in code.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.accounting.permissions import ROLE_PERMISSIONS, sync_role_groups
from apps.organizations.permissions import role_group_name


class Command(BaseCommand):
    help = "Synchronise accounting permissions into the role groups."

    def handle(self, *args: Any, **options: Any) -> None:
        sync_role_groups()

        verbosity = options.get("verbosity", 1)
        if verbosity < 1:
            return

        for role in sorted(ROLE_PERMISSIONS):
            granted = sorted(ROLE_PERMISSIONS[role])
            self.stdout.write(self.style.SUCCESS(role_group_name(role)))
            if not granted:
                self.stdout.write("    (no accounting authority)")
                continue
            for permission in granted:
                self.stdout.write(f"    {permission}")
