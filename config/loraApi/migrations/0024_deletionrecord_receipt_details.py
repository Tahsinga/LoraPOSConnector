from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0023_productcatalog_branch_confirmed_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='deletionrecord',
            name='receipt_products',
            field=models.TextField(blank=True, default='[]'),
        ),
        migrations.AddField(
            model_name='deletionrecord',
            name='receipt_total',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
    ]