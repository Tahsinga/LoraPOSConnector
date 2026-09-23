from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0016_stocktransfer_branch_stock_applied'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransfer',
            name='target_quantity',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
    ]