from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0018_productcatalog_pending_price_update'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name='productcatalog',
                    name='pending_selling_price',
                    field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
                ),
                migrations.AddField(
                    model_name='productcatalog',
                    name='tax_rate',
                    field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
                ),
                migrations.AddField(
                    model_name='productcatalog',
                    name='pending_product_creation',
                    field=models.BooleanField(default=False),
                ),
            ],
        ),
    ]