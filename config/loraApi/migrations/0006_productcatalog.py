from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0005_stocktransfer_product_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductCatalog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('product_id', models.IntegerField(unique=True)),
                ('product_name', models.CharField(blank=True, default='', max_length=250)),
                ('product_code', models.CharField(blank=True, default='', max_length=50)),
                ('barcode', models.CharField(blank=True, default='', max_length=100)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['product_name', 'product_id'],
            },
        ),
    ]
