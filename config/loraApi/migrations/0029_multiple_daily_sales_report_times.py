from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0028_salesreportschedule'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='salesreportschedule',
            options={'ordering': ['branch', 'report_time']},
        ),
        migrations.AlterField(
            model_name='salesreportschedule',
            name='branch',
            field=models.CharField(max_length=255),
        ),
        migrations.AddConstraint(
            model_name='salesreportschedule',
            constraint=models.UniqueConstraint(fields=('branch', 'report_time'), name='uniq_sales_report_branch_time'),
        ),
    ]