from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0007_branch_product_catalog'),
    ]

    operations = [
        migrations.CreateModel(
            name='StockMovement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(blank=True, default='', max_length=255)),
                ('product_id', models.IntegerField()),
                ('product_name', models.CharField(blank=True, default='', max_length=255)),
                ('movement_type', models.CharField(choices=[('received', 'Received'), ('sold', 'Sold')], max_length=20)),
                ('quantity', models.DecimalField(decimal_places=3, max_digits=18)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('source', models.CharField(blank=True, default='', max_length=100)),
            ],
            options={'ordering': ['created_at']},
        ),
    ]