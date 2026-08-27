# Generated manually because this migration only changes Django model options.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0005_role_definitions_and_open_role_keys"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="organization",
            options={
                "ordering": ["code"],
                "permissions": (
                    ("manage_users", "Can manage user accounts in this organization"),
                    ("manage_access", "Can manage access grants in this organization"),
                    ("manage_roles", "Can manage roles in this organization"),
                    ("view_audit", "Can view audit events in this organization"),
                    ("manage_org_settings", "Can manage organization settings"),
                ),
                "verbose_name": "organization",
                "verbose_name_plural": "organizations",
            },
        ),
    ]
