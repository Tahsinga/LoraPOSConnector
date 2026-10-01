from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0027_salesreportrequest_scheduled_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='SalesReportSchedule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(max_length=255, unique=True)),
                ('report_time', models.TimeField()),
                ('timezone', models.CharField(default='UTC', max_length=64)),
                ('last_queued_date', models.DateField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['branch']},
        ),
    ]