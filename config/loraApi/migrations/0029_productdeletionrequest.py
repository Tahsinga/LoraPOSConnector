from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0028_salesreportschedule'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductDeletionRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(max_length=255)),
                ('product_id', models.IntegerField()),
                ('product_name', models.CharField(blank=True, default='', max_length=250)),
                ('requested_by', models.CharField(blank=True, default='', max_length=255)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=20)),
                ('error', models.TextField(blank=True, default='')),
                ('requested_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['requested_at'],
            },
        ),
        migrations.AddIndex(
            model_name='productdeletionrequest',
            index=models.Index(fields=['branch', 'status', 'updated_at'], name='proddelete_branch_status_idx'),
        ),
    ]