from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models


def round_stock_quantities(apps, schema_editor):
    model_names = ('MainStockBalance', 'StockTransfer', 'StockMovement', 'ProductCatalog')
    fields = ('quantity', 'available_quantity', 'sold_quantity')
    for model_name in model_names:
        model = apps.get_model('loraApi', model_name)
        field_names = {field.name for field in model._meta.fields}
        for item in model.objects.all().iterator():
            updates = {}
            for field_name in fields:
                if field_name in field_names:
                    value = getattr(item, field_name)
                    if value is not None:
                        updates[field_name] = Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            if updates:
                model.objects.filter(pk=item.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0012_productcatalog_sold_quantity'),
    ]

    operations = [
        migrations.RunPython(round_stock_quantities, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='mainstockbalance',
            name='quantity',
            field=models.DecimalField(decimal_places=0, default=0, max_digits=18),
        ),
        migrations.AlterField(
            model_name='stocktransfer',
            name='quantity',
            field=models.DecimalField(decimal_places=0, max_digits=18),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='quantity',
            field=models.DecimalField(decimal_places=0, max_digits=18),
        ),
        migrations.AlterField(
            model_name='productcatalog',
            name='available_quantity',
            field=models.DecimalField(decimal_places=0, default=0, max_digits=18),
        ),
        migrations.AlterField(
            model_name='productcatalog',
            name='sold_quantity',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
    ]
