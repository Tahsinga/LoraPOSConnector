from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0010_productcatalog_available_quantity'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='selling_price',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=18),
        ),
    ]
