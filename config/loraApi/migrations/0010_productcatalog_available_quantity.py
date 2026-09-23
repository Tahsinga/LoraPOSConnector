from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0009_stocktransfer_claimed_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='available_quantity',
            field=models.DecimalField(decimal_places=3, default=0, max_digits=18),
        ),
    ]