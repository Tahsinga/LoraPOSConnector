from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0002_deletionrecord_deleted_by'),
    ]

    operations = [
        migrations.CreateModel(
            name='SalesReportRequest',
            fields=[
                ('request_id', models.CharField(max_length=255, primary_key=True, serialize=False)),
                ('branch', models.CharField(max_length=255)),
                ('report_date', models.DateField()),
                ('status', models.CharField(default='pending', max_length=20)),
                ('requested_by', models.CharField(blank=True, default='', max_length=255)),
                ('requested_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('row_count', models.IntegerField(blank=True, null=True)),
                ('error_message', models.TextField(blank=True, default='')),
            ],
            options={
                'ordering': ['-requested_at'],
            },
        ),
    ]
