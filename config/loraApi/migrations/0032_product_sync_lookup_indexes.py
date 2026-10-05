from django.db import migrations, models
from django.db.models.functions import Upper


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0031_merge_productdeletionrequest_salesreporttime'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='stockmovement',
            index=models.Index(
                Upper('branch'), 'product_id', 'movement_type', models.F('created_at').desc(),
                name='sm_upper_branch_prod_type_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='productcatalog',
            index=models.Index(
                Upper('branch'), 'product_id',
                name='pc_upper_branch_product_idx',
            ),
        ),
    ]