from django.db import migrations


def add_missing_catalog_columns(apps, schema_editor):
    table_name = schema_editor.quote_name('loraApi_productcatalog')
    existing_columns = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(),
            'loraApi_productcatalog',
        )
    }
    columns = {
        'pending_selling_price': 'decimal NULL',
        'tax_rate': 'decimal NOT NULL DEFAULT 0',
        'pending_product_creation': 'boolean NOT NULL DEFAULT FALSE',
    }
    for column_name, definition in columns.items():
        if column_name not in existing_columns:
            schema_editor.execute(
                f'ALTER TABLE {table_name} ADD COLUMN {schema_editor.quote_name(column_name)} {definition}'
            )


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0019_productcatalog_live_fields'),
    ]

    operations = [
        migrations.RunPython(add_missing_catalog_columns, migrations.RunPython.noop),
    ]
