from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0014_alter_stockmovement_movement_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='pending_stock_adjustment',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='productcatalog',
            name='pending_stock_quantity',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
    ]
