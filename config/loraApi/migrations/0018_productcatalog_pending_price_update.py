from django.db import migrations, models


def add_pending_price_update_column(apps, schema_editor):
    table_name = schema_editor.quote_name('loraApi_productcatalog')
    column_name = schema_editor.quote_name('pending_price_update')
    columns = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(),
            'loraApi_productcatalog',
        )
    }
    if 'pending_price_update' not in columns:
        schema_editor.execute(
            f'ALTER TABLE {table_name} ADD COLUMN {column_name} boolean NOT NULL DEFAULT 0'
        )


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0017_stocktransfer_target_quantity'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    add_pending_price_update_column,
                    migrations.RunPython.noop,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='productcatalog',
                    name='pending_price_update',
                    field=models.BooleanField(default=False),
                ),
            ],
        ),
    ]
