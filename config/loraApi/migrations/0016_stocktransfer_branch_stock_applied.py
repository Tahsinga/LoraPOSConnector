from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0015_productcatalog_pending_stock_adjustment'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransfer',
            name='branch_stock_applied',
            field=models.BooleanField(default=False),
        ),
    ]