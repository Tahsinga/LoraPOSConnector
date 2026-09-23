from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0003_salesreportrequest'),
    ]

    operations = [
        migrations.CreateModel(
            name='MainStockBalance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('product_id', models.IntegerField(unique=True)),
                ('quantity', models.DecimalField(decimal_places=3, default=0, max_digits=18)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['product_id'],
            },
        ),
        migrations.CreateModel(
            name='StockTransfer',
            fields=[
                ('transfer_id', models.CharField(max_length=255, primary_key=True, serialize=False)),
                ('branch', models.CharField(max_length=255)),
                ('product_id', models.IntegerField()),
                ('quantity', models.DecimalField(decimal_places=3, max_digits=18)),
                ('status', models.CharField(default='pending', max_length=20)),
                ('created_by', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
    ]
