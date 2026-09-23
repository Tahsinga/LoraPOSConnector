from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0013_integer_stock_quantities'),
    ]

    operations = [
        migrations.AlterField(
            model_name='stockmovement',
            name='movement_type',
            field=models.CharField(
                choices=[('received', 'Received'), ('sold', 'Sold'), ('adjusted', 'Stock adjusted')],
                max_length=20,
            ),
        ),
    ]
