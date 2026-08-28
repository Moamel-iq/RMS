from django.db import migrations


MODELS = ("RecipeCategory", "Recipe", "RecipeServing", "RecipeCostSnapshotServing", "HistoricalRecipeCategory", "HistoricalRecipe", "HistoricalRecipeServing")


def rename_legacy_name_columns(apps, schema_editor):
    for model_name in MODELS:
        model = apps.get_model("kitchen", model_name)
        columns = {column.name for column in schema_editor.connection.introspection.get_table_description(schema_editor.connection.cursor(), model._meta.db_table)}
        if "name_ar" in columns and "name" not in columns:
            quote = schema_editor.quote_name
            schema_editor.execute(f"ALTER TABLE {quote(model._meta.db_table)} RENAME COLUMN {quote('name_ar')} TO {quote('name')}")


class Migration(migrations.Migration):
    dependencies = [("kitchen", "0023_batch_document_link_guards")]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(rename_legacy_name_columns, migrations.RunPython.noop)],
            state_operations=[
                migrations.RenameField(model_name=model_name, old_name="name_ar", new_name="name")
                for model_name in ("recipecategory", "recipe", "recipeserving", "recipecostsnapshotserving", "historicalrecipecategory", "historicalrecipe", "historicalrecipeserving")
            ],
        )
    ]
