from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('loraApi', '0022_invoicereprintrequest'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name='productcatalog',
                    name='branch_confirmed',
                    field=models.BooleanField(default=True),
                ),
            ],
        ),
    ]
