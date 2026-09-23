from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0004_mainstockbalance_stocktransfer'),
    ]

    operations = [
        migrations.AddField(
            model_name='mainstockbalance',
            name='product_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='stocktransfer',
            name='product_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
