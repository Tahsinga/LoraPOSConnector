from django.db import models
from django.db.models.functions import Upper


class DeletionRecord(models.Model):
	deletion_id = models.CharField(max_length=255, primary_key=True)
	branch = models.CharField(max_length=255, blank=True, default='')
	invoice = models.CharField(max_length=255)
	product_id = models.CharField(max_length=255, blank=True, null=True)
	entry_no = models.CharField(max_length=255, blank=True, default='')
	action = models.CharField(max_length=100, default='delete')
	status = models.CharField(max_length=20, default='pending')
	timestamp = models.DateTimeField(auto_now_add=True)
	source = models.CharField(max_length=100, default='unknown')
	deleted_from_main = models.BooleanField(default=False)
	message = models.TextField(blank=True, default='')
	receipt_products = models.TextField(blank=True, default='[]')
	receipt_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
	deleted_rows = models.IntegerField(null=True, blank=True)
	deleted_by = models.CharField(max_length=255, blank=True, default='')
	confirmed_branch = models.CharField(max_length=255, blank=True, null=True)
	confirmation_timestamp = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ['timestamp']
		indexes = [
			models.Index(fields=['status', 'timestamp']),
			models.Index(fields=['status', 'confirmation_timestamp']),
		]


class SalesReportRequest(models.Model):
	request_id = models.CharField(max_length=255, primary_key=True)
	branch = models.CharField(max_length=255)
	report_date = models.DateField()
	scheduled_at = models.DateTimeField(null=True, blank=True)
	status = models.CharField(max_length=20, default='pending')
	requested_by = models.CharField(max_length=255, blank=True, default='')
	requested_at = models.DateTimeField(auto_now_add=True)
	completed_at = models.DateTimeField(null=True, blank=True)
	row_count = models.IntegerField(null=True, blank=True)
	error_message = models.TextField(blank=True, default='')

	class Meta:
		ordering = ['-requested_at']


class SalesReportSchedule(models.Model):
	branch = models.CharField(max_length=255)
	report_time = models.TimeField()
	timezone = models.CharField(max_length=64, default='UTC')
	last_queued_date = models.DateField(null=True, blank=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['branch', 'report_time']
		constraints = [
			models.UniqueConstraint(fields=['branch', 'report_time'], name='uniq_sales_report_branch_time'),
		]


class InvoiceReprintRequest(models.Model):
	request_id = models.CharField(max_length=255, primary_key=True)
	branch = models.CharField(max_length=255)
	invoice = models.CharField(max_length=255)
	status = models.CharField(max_length=20, default='pending')
	requested_by = models.CharField(max_length=255, blank=True, default='')
	requested_at = models.DateTimeField(auto_now_add=True)
	completed_at = models.DateTimeField(null=True, blank=True)
	error_message = models.TextField(blank=True, default='')

	class Meta:
		ordering = ['requested_at']


class MainStockBalance(models.Model):
	product_id = models.IntegerField(unique=True)
	product_name = models.CharField(max_length=255, blank=True, default='')
	quantity = models.DecimalField(max_digits=18, decimal_places=0, default=0)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['product_id']


class StockTransfer(models.Model):
	transfer_id = models.CharField(max_length=255, primary_key=True)
	branch = models.CharField(max_length=255)
	product_id = models.IntegerField()
	product_name = models.CharField(max_length=255, blank=True, default='')
	quantity = models.DecimalField(max_digits=18, decimal_places=0)
	target_quantity = models.DecimalField(max_digits=18, decimal_places=0, null=True, blank=True)
	status = models.CharField(max_length=20, default='pending')
	created_by = models.CharField(max_length=255, blank=True, default='')
	created_at = models.DateTimeField(auto_now_add=True)
	claimed_at = models.DateTimeField(null=True, blank=True)
	completed_at = models.DateTimeField(null=True, blank=True)
	branch_stock_applied = models.BooleanField(default=False)

	class Meta:
		ordering = ['created_at']
		indexes = [
			models.Index(fields=['branch', 'created_at']),
			models.Index(fields=['branch', 'product_id', 'created_at']),
		]


class StockMovement(models.Model):
	MOVEMENT_TYPES = [('received', 'Received'), ('sold', 'Sold'), ('adjusted', 'Stock adjusted')]
	branch = models.CharField(max_length=255, blank=True, default='')
	product_id = models.IntegerField()
	product_name = models.CharField(max_length=255, blank=True, default='')
	movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)
	quantity = models.DecimalField(max_digits=18, decimal_places=0)
	created_at = models.DateTimeField(auto_now_add=True)
	source = models.CharField(max_length=100, blank=True, default='')

	class Meta:
		ordering = ['created_at']
		indexes = [
			models.Index(fields=['created_at']),
			models.Index(fields=['branch', 'created_at']),
			models.Index(Upper('branch'), 'product_id', 'movement_type', models.F('created_at').desc(), name='sm_upper_branch_prod_type_idx'),
		]


class ProductCatalog(models.Model):
	branch = models.CharField(max_length=255, default='')
	product_id = models.IntegerField()
	product_name = models.CharField(max_length=250, blank=True, default='')
	product_code = models.CharField(max_length=50, blank=True, default='')
	barcode = models.CharField(max_length=100, blank=True, default='')
	available_quantity = models.DecimalField(max_digits=18, decimal_places=0, default=0)
	selling_price = models.DecimalField(max_digits=18, decimal_places=2, default=0)
	sold_quantity = models.DecimalField(max_digits=18, decimal_places=0, null=True, blank=True)
	branch_confirmed = models.BooleanField(default=True)
	pending_price_update = models.BooleanField(default=False)
	pending_selling_price = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
	tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
	pending_product_creation = models.BooleanField(default=False)
	pending_stock_adjustment = models.BooleanField(default=False)
	pending_stock_quantity = models.DecimalField(max_digits=18, decimal_places=0, null=True, blank=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['product_name', 'product_id']
		indexes = [
			models.Index(fields=['branch', 'branch_confirmed']),
			models.Index(fields=['branch', 'product_name']),
			models.Index(Upper('branch'), 'product_id', name='pc_upper_branch_product_idx'),
		]
		constraints = [
			models.UniqueConstraint(fields=['branch', 'product_id'], name='unique_branch_product'),
		]


class ProductDeletionRequest(models.Model):
	STATUS_CHOICES = [('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed')]
	branch = models.CharField(max_length=255)
	product_id = models.IntegerField()
	product_name = models.CharField(max_length=250, blank=True, default='')
	requested_by = models.CharField(max_length=255, blank=True, default='')
	status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
	error = models.TextField(blank=True, default='')
	requested_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['requested_at']
		indexes = [
			models.Index(fields=['branch', 'status', 'updated_at'], name='proddelete_branch_status_idx'),
		]


class BranchHeartbeat(models.Model):
	branch = models.CharField(max_length=255, unique=True)
	device_role = models.CharField(max_length=50, default='Branch PC')
	last_seen = models.DateTimeField()

	class Meta:
		ordering = ['branch']
		indexes = [
			models.Index(fields=['last_seen']),
		]
