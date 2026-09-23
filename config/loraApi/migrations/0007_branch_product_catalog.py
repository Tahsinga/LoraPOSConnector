from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0006_productcatalog'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='branch',
            field=models.CharField(default='', max_length=255),
        ),
        migrations.AlterField(
            model_name='productcatalog',
            name='product_id',
            field=models.IntegerField(),
        ),
        migrations.AddConstraint(
            model_name='productcatalog',
            constraint=models.UniqueConstraint(fields=('branch', 'product_id'), name='unique_branch_product'),
        ),
    ]
