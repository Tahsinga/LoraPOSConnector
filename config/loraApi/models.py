from django.db import models


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
	deleted_rows = models.IntegerField(null=True, blank=True)
	deleted_by = models.CharField(max_length=255, blank=True, default='')
	confirmed_branch = models.CharField(max_length=255, blank=True, null=True)
	confirmation_timestamp = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ['timestamp']


class SalesReportRequest(models.Model):
	request_id = models.CharField(max_length=255, primary_key=True)
	branch = models.CharField(max_length=255)
	report_date = models.DateField()
	status = models.CharField(max_length=20, default='pending')
	requested_by = models.CharField(max_length=255, blank=True, default='')
	requested_at = models.DateTimeField(auto_now_add=True)
	completed_at = models.DateTimeField(null=True, blank=True)
	row_count = models.IntegerField(null=True, blank=True)
	error_message = models.TextField(blank=True, default='')

	class Meta:
		ordering = ['-requested_at']
