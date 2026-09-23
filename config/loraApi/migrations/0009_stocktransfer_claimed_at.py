from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0008_stockmovement'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransfer',
            name='claimed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]