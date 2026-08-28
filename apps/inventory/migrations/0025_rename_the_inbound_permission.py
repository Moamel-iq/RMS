"""
Rename `post_receipt` in the auth table to what it actually authorizes.

Django creates permissions from `Meta.permissions` and never renames one whose
label changed, so the row still read "Can post a stock receipt" — the name of a
screen that no longer exists. The permission itself stayed on purpose: a
purchase goods receipt, a transfer in, a kitchen production output, a count
gain and a manual adjustment all post through it, so it never belonged to the
un-invoiced receipt alone. Only the label was ever wrong.
"""

from django.db import migrations

OLD = "Can post a stock receipt"
NEW = "Can post stock into a warehouse"


def rename(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="inventory", codename="post_receipt"
    ).update(name=NEW)


def unrename(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="inventory", codename="post_receipt"
    ).update(name=OLD)


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0024_drop_the_return_source_column"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(rename, unrename, elidable=False)]
