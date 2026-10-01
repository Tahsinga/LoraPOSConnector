from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0026_branchheartbeat'),
    ]

    operations = [
        migrations.AddField(
            model_name='salesreportrequest',
            name='scheduled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]