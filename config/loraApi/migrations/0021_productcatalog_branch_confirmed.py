from django.db import migrations


def add_branch_confirmation_column(apps, schema_editor):
    table_name = schema_editor.quote_name('loraApi_productcatalog')
    existing_columns = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(),
            'loraApi_productcatalog',
        )
    }
    if 'branch_confirmed' not in existing_columns:
        schema_editor.execute(
            f'ALTER TABLE {table_name} ADD COLUMN {schema_editor.quote_name("branch_confirmed")} boolean NOT NULL DEFAULT 1'
        )


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0020_productcatalog_live_fields_db'),
    ]

    operations = [
        migrations.RunPython(add_branch_confirmation_column, migrations.RunPython.noop),
    ]
