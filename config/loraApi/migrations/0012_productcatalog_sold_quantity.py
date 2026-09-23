from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0011_productcatalog_selling_price'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='sold_quantity',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=18, null=True),
        ),
    ]
