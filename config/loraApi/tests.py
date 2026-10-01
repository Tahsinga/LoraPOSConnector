import gzip
from datetime import datetime, timedelta, timezone as datetime_timezone
import re
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from loraApi.state_store import load_state
from django.utils import timezone
from loraApi.models import BranchHeartbeat, InvoiceReprintRequest, MainStockBalance, ProductCatalog, ProductDeletionRequest, SalesReportRequest, SalesReportSchedule, StockMovement, StockTransfer
import json


class BandwidthCompressionTests(TestCase):
	def setUp(self):
		admin = get_user_model().objects.create_superuser(username='compression-admin', password='CompressionPass4182!')
		self.client.force_login(admin)
		for product_id in range(500, 520):
			ProductCatalog.objects.create(
				branch='MAIN', product_id=product_id,
				product_name=f'Repeatable product name {product_id}',
				product_code=f'CODE-{product_id}', barcode=f'BARCODE-{product_id}',
			)

	def test_product_catalog_is_compressed_without_changing_its_payload(self):
		response = self.client.get('/api/products/?branch=MAIN', HTTP_ACCEPT_ENCODING='gzip')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.headers.get('Content-Encoding'), 'gzip')
		decompressed = gzip.decompress(response.content)
		self.assertEqual(json.loads(decompressed)['status'], 'ok')
		self.assertEqual(len(json.loads(decompressed)['products']), 20)
		self.assertLess(len(response.content), len(decompressed))


class ProductCatalogSyncTests(TestCase):
	def test_sync_collapses_duplicate_case_variants_and_remains_idempotent(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=8001, product_name='Catalog Product', available_quantity=4,
		)
		ProductCatalog.objects.create(
			branch='brancha', product_id=8001, product_name='Catalog Product', available_quantity=3,
		)
		payload = json.dumps({
			'branch': 'BranchA',
			'products': [{
				'product_id': 8001,
				'product_name': 'Catalog Product',
				'available_quantity': 12,
				'selling_price': '11.00',
				'tax_rate': '5.00',
			}],
		})

		first = self.client.post('/api/products/sync/', data=payload, content_type='application/json')
		second = self.client.post('/api/products/sync/', data=payload, content_type='application/json')

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 200)
		self.assertEqual(ProductCatalog.objects.filter(branch__iexact='BranchA', product_id=8001).count(), 1)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=8001).available_quantity, 12)


class ProductDeletionTests(TestCase):
	def setUp(self):
		self.password = 'ProductDeletePass4182!'
		admin = get_user_model().objects.create_superuser(username='product-delete-admin', password=self.password)
		self.client.force_login(admin)
		ProductCatalog.objects.create(branch='BranchA', product_id=3001, product_name='Branch Product')
		ProductCatalog.objects.create(branch='BranchB', product_id=3001, product_name='Branch Product')
		ProductCatalog.objects.create(branch='MAIN', product_id=3001, product_name='Branch Product')

	def test_deletion_requires_the_current_users_password(self):
		for password in ['', 'not-the-password']:
			with self.subTest(password='missing' if not password else 'incorrect'):
				response = self.client.post(
					'/api/products/delete/',
					data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': password}),
					content_type='application/json',
				)
				self.assertEqual(response.status_code, 403)
		self.assertFalse(ProductDeletionRequest.objects.exists())

	def test_deletion_is_queued_polled_and_completed_for_only_one_branch(self):
		response = self.client.post(
			'/api/products/delete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': self.password}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		request_id = response.json()['request_id']
		self.assertEqual(self.client.get('/api/products/?branch=BranchA').json()['products'], [])
		self.assertEqual(self.client.get('/api/branch-sync/?branch=BranchA').json()['pending_product_deletions'], [{
			'request_id': request_id,
			'branch': 'BranchA',
			'product_id': 3001,
			'product_name': 'Branch Product',
		}])

		complete = self.client.post(
			'/api/products/delete/complete/',
			data=json.dumps({'request_id': request_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(complete.status_code, 200)
		self.assertEqual(ProductDeletionRequest.objects.get(pk=request_id).status, 'completed')
		self.assertFalse(ProductCatalog.objects.filter(branch='BranchA', product_id=3001).exists())
		self.assertTrue(ProductCatalog.objects.filter(branch='BranchB', product_id=3001).exists())
		self.assertTrue(ProductCatalog.objects.filter(branch='MAIN', product_id=3001).exists())

	def test_deleted_products_page_and_api_show_completed_deletions(self):
		completed = ProductDeletionRequest.objects.create(
			branch='BranchA', product_id=3001, product_name='Branch Product',
			requested_by='product-delete-admin', status='completed',
		)
		ProductDeletionRequest.objects.create(
			branch='BranchB', product_id=3001, product_name='Pending product', status='pending',
		)

		page = self.client.get('/products/deleted/')
		response = self.client.get('/api/products/deleted/')

		self.assertEqual(page.status_code, 200)
		self.assertContains(page, 'Deleted products')
		self.assertEqual(response.json()['products'][0]['product_name'], 'Branch Product')
		self.assertEqual(len(response.json()['products']), 1)

	@override_settings(STORAGES={
		'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
		'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
	})
	def test_stock_page_shows_delete_password_tax_and_history_controls(self):
		response = self.client.get('/stock/')

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Delete at branch')
		self.assertContains(response, 'Your account password')
		self.assertContains(response, 'Save tax')
		self.assertContains(response, '/products/deleted/')


class ProductTaxRateTests(TestCase):
	def setUp(self):
		admin = get_user_model().objects.create_superuser(username='product-tax-admin', password='ProductTaxPass4182!')
		self.client.force_login(admin)

	def test_tax_rate_updates_confirmed_products_and_shared_pos_feed(self):
		for branch in ['BranchA', 'BranchB', 'MAIN']:
			ProductCatalog.objects.create(branch=branch, product_id=401, product_name='Tax Product', tax_rate='5.00')
		ProductCatalog.objects.create(
			branch='BranchPending', product_id=401, product_name='Tax Product',
			tax_rate='5.00', branch_confirmed=False,
		)
		response = self.client.post(
			'/api/products/tax-rate/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 401, 'tax_rate': '7.50'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		for branch in ['BranchA', 'BranchB', 'MAIN']:
			self.assertEqual(str(ProductCatalog.objects.get(branch=branch, product_id=401).tax_rate), '7.50')
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchPending', product_id=401).tax_rate), '5.00')
		shared = next(item for item in self.client.get('/api/products/shared/').json()['products'] if item['product_id'] == 401)
		self.assertEqual(shared['tax_rate'], '7.50')

	def test_tax_rate_rejects_values_over_one_hundred(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=402, product_name='Tax Product')
		response = self.client.post(
			'/api/products/tax-rate/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 402, 'tax_rate': '100.01'}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 400)
		self.assertEqual(str(ProductCatalog.objects.get(product_id=402).tax_rate), '0.00')


@override_settings(STORAGES={
	'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
	'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class StockTransferTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser(
			username='transfer-admin', password='TransferPass4182!'
		)
		self.client.force_login(self.admin)
		MainStockBalance.objects.create(product_id=999, product_name='Test Product', quantity=10)

	def test_sign_out_works_from_each_page_with_csrf_enforced(self):
		client = self.client_class(enforce_csrf_checks=True)
		for page in ('/', '/stock/', '/history/', '/stock/movements/history/', '/users/'):
			client.force_login(self.admin)
			page_response = client.get(page)
			self.assertEqual(page_response.status_code, 200, page)
			logout_form = re.search(
				r'<form[^>]*action="/logout/"[^>]*>(.*?)</form>',
				page_response.content.decode(),
				re.DOTALL,
			)
			self.assertIsNotNone(logout_form, page)
			csrf_token = re.search(
				r'name="csrfmiddlewaretoken" value="([^"]+)"',
				logout_form.group(1),
			)
			self.assertIsNotNone(csrf_token, page)
			logout_response = client.post('/logout/', {'csrfmiddlewaretoken': csrf_token.group(1)})
			self.assertRedirects(logout_response, '/login/', fetch_redirect_response=False)

	@override_settings(STORAGES={
		'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
		'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
	})
	def test_bandwidth_page_and_dashboard_link(self):
		self.client.logout()
		self.assertEqual(self.client.get('/bandwidth/').status_code, 302)
		self.client.force_login(self.admin)
		self.assertContains(self.client.get('/'), 'id="api-bandwidth-meter" href="/bandwidth/"')
		page = self.client.get('/bandwidth/')
		self.assertContains(page, 'API data received today')
		self.assertContains(page, 'Daily log')
		self.assertContains(page, 'data-bandwidth-month-log')

	def test_main_stock_update_sets_stock_take_value(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=7,
		)
		response = self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': 20, 'branch': 'BranchA'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['quantity'], '20')
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 20)
		stock_take = StockTransfer.objects.get()
		self.assertEqual(stock_take.target_quantity, 20)
		self.assertEqual(stock_take.quantity, 20)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 7)
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': stock_take.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 20)
		movement = StockMovement.objects.get(product_id=999, branch='BranchA')
		self.assertEqual(movement.movement_type, 'adjusted')
		self.assertEqual(movement.quantity, 20)

	def test_main_stock_update_rejects_negative_stock_take(self):
		response = self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': -1}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 10)

	def test_transfer_is_only_claimed_by_its_branch(self):
		response = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 5}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)

		wrong_branch = self.client.get('/api/branch-sync/?branch=TASHINGA')
		self.assertEqual(wrong_branch.json()['pending_transfers'], [])
		self.assertEqual(StockTransfer.objects.get().status, 'pending')

		branch_response = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(branch_response.json()['pending_transfers'][0]['branch'], 'BranchA')
		self.assertEqual(StockTransfer.objects.get().status, 'pending')
		self.assertIsNotNone(StockTransfer.objects.get().claimed_at)
		second_poll = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(second_poll.json()['pending_transfers'], [])

	def test_direct_send_adds_quantity_to_main_and_branch(self):
		MainStockBalance.objects.filter(product_id=999).update(quantity=150)
		response = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 100}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 250)
		self.assertFalse(ProductCatalog.objects.filter(branch='BranchA', product_id=999).exists())
		self.assertEqual(StockTransfer.objects.get().quantity, 100)

	def test_transfer_adds_to_main_and_branch_and_marks_branch_target_pending(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		response = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 5}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 10)
		transfer = StockTransfer.objects.get()
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		catalog = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 15)
		self.assertEqual(catalog.available_quantity, 15)
		self.assertTrue(catalog.pending_stock_adjustment)
		self.assertEqual(catalog.pending_stock_quantity, 15)
		self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'BranchA', 'entered_by': 'branch-user', 'products': [{
				'product_id': 999, 'product_name': 'Test Product', 'available_quantity': 10,
			}]}),
			content_type='application/json',
		)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 15)

	def test_stock_take_updates_main_and_branch_together(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=4,
		)
		response = self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': 12, 'branch': 'BranchA'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 12)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 4)
		stock_take = StockTransfer.objects.get()
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': stock_take.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 12)
		self.assertFalse(StockMovement.objects.filter(product_id=999, movement_type='sold').exists())

	def test_sale_after_stock_take_updates_web_quantity(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=4,
		)
		stock_take = self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': 12, 'branch': 'BranchA'}),
			content_type='application/json',
		)
		self.assertEqual(stock_take.status_code, 200)
		stock_take_transfer = StockTransfer.objects.get()
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': stock_take_transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)

		sync = self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'BranchA', 'products': [{
				'product_id': 999, 'product_name': 'Test Product', 'available_quantity': 11, 'sold_quantity': 1,
			}]}),
			content_type='application/json',
		)
		self.assertEqual(sync.status_code, 200)
		catalog = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		self.assertEqual(catalog.available_quantity, 11)
		self.assertFalse(catalog.pending_stock_adjustment)
		self.assertEqual(StockMovement.objects.get(product_id=999, movement_type='sold').quantity, 1)

	def test_repeated_success_acknowledgment_is_idempotent(self):
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_TEST', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)
		payload = json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True})

		first = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')
		completed_at = StockTransfer.objects.get().completed_at
		second = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 200)
		self.assertEqual(StockTransfer.objects.get().status, 'completed')
		self.assertEqual(StockTransfer.objects.get().completed_at, completed_at)

	def test_successful_transfer_updates_branch_available_stock_once(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_AVAILABLE', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)
		payload = json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True})

		first = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')
		second = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 200)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 15)

	def test_branch_price_is_queued_for_all_confirmed_branches(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', selling_price='10.00',
		)
		ProductCatalog.objects.create(
			branch='BranchB', product_id=999, product_name='Test Product', selling_price='10.00',
		)
		ProductCatalog.objects.create(
			branch='BranchPending', product_id=999, product_name='Test Product', selling_price='10.00',
			branch_confirmed=False,
		)
		ProductCatalog.objects.create(
			branch='MAIN', product_id=999, product_name='Test Product', selling_price='10.00',
		)
		queued = self.client.post(
			'/api/stock/prices/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'selling_price': '12.50'}),
			content_type='application/json',
		)

		self.assertEqual(queued.status_code, 202)
		self.assertEqual(queued.json()['branch_count'], 2)
		self.assertCountEqual(queued.json()['branches'], ['BranchA', 'BranchB'])
		for branch in ['BranchA', 'BranchB']:
			catalog = ProductCatalog.objects.get(branch=branch, product_id=999)
			self.assertEqual(catalog.selling_price, 10)
			self.assertTrue(catalog.pending_price_update)
			self.assertEqual(catalog.pending_selling_price, 12.5)

		confirmed = self.client.post(
			'/api/stock/prices/complete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(confirmed.status_code, 200)
		branch_a = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		branch_b = ProductCatalog.objects.get(branch='BranchB', product_id=999)
		self.assertEqual(branch_a.selling_price, 12.5)
		self.assertFalse(branch_a.pending_price_update)
		self.assertEqual(branch_b.selling_price, 10)
		self.assertTrue(branch_b.pending_price_update)
		self.assertFalse(ProductCatalog.objects.get(branch='BranchPending', product_id=999).pending_price_update)
		self.assertFalse(ProductCatalog.objects.get(branch='MAIN', product_id=999).pending_price_update)

	def test_stock_summary_counts_queued_transfer_as_sent(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=0,
		)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_QUEUED', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)

		response = self.client.get('/api/stock/summary/?branch=brancha')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['products'][0]['sent_quantity'], '5')

	def test_stock_transfer_log_filters_by_creation_date(self):
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_DATE_FILTER', branch='BranchA', product_id=1001,
			product_name='Date Filter Product', quantity=3,
		)
		selected_date = timezone.localdate() - timedelta(days=1)
		created_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time()))
		StockTransfer.objects.filter(pk=transfer.pk).update(created_at=created_at)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_DATE_OTHER', branch='BranchA', product_id=1002,
			product_name='Other Date Product', quantity=2,
		)

		response = self.client.get('/api/stock/transfers/log/', {'date': selected_date.isoformat()})

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['date'], selected_date.isoformat())
		self.assertEqual([item['transfer_id'] for item in response.json()['transfers']], ['TRANSFER_DATE_FILTER'])

	def test_stock_summary_returns_only_changed_products(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=1001, product_name='Test Product')
		ProductCatalog.objects.filter(product_id=1001).update(updated_at=timezone.now() - timedelta(minutes=10))
		no_changes = self.client.get('/api/stock/summary/', {
			'branch': 'BranchA',
			'since': (timezone.now() - timedelta(minutes=1)).isoformat(),
		})
		self.assertFalse(no_changes.json()['changed'])
		self.assertEqual(no_changes.json()['products'], [])

		change_start = timezone.now()
		product = ProductCatalog.objects.get(product_id=1001)
		product.available_quantity = 7
		product.save()
		changed = self.client.get('/api/stock/summary/', {
			'branch': 'BranchA',
			'since': change_start.isoformat(),
		})
		self.assertFalse(changed.json()['full'])
		self.assertEqual([item['product_id'] for item in changed.json()['products']], [1001])

	def test_product_sync_inbox_returns_only_changed_products(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=1101, product_name='Test Product')
		ProductCatalog.objects.filter(product_id=1101).update(updated_at=timezone.now() - timedelta(minutes=10))
		no_changes = self.client.get('/api/products/inbox/', {
			'since': (timezone.now() - timedelta(minutes=1)).isoformat(),
		})
		self.assertFalse(no_changes.json()['changed'])
		self.assertEqual(no_changes.json()['products'], [])

		change_start = timezone.now()
		product = ProductCatalog.objects.get(product_id=1101)
		product.available_quantity = 8
		product.save()
		changed = self.client.get('/api/products/inbox/', {'since': change_start.isoformat()})
		self.assertFalse(changed.json()['full'])
		self.assertEqual([item['product_id'] for item in changed.json()['products']], [1101])

	def test_shared_product_catalog_exposes_products_for_branch_sync(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1101, product_name='Branch Product',
			product_code='B-1101', barcode='111', selling_price='8.00', tax_rate='5.00',
		)
		ProductCatalog.objects.create(
			branch='MAIN', product_id=1101, product_name='Canonical Product',
			product_code='M-1101', barcode='222', selling_price='10.00', tax_rate='7.50',
		)

		response = self.client.get('/api/products/shared/')

		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()['full'])
		self.assertEqual(response.json()['products'], [{
			'product_id': 1101,
			'product_name': 'Canonical Product',
			'product_code': 'M-1101',
			'barcode': '222',
			'selling_price': '10.00',
			'tax_rate': '7.50',
		}])

	def test_branch_tax_rate_sync_updates_shared_catalog(self):
		ProductCatalog.objects.create(
			branch='MAIN', product_id=1301, product_name='Taxed Product', tax_rate='0.00',
		)
		response = self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'BranchA', 'products': [{
				'product_id': 1301,
				'product_name': 'Taxed Product',
				'available_quantity': 4,
				'selling_price': '10.00',
				'tax_rate': '7.50',
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=1301).tax_rate), '7.50')
		self.assertEqual(str(ProductCatalog.objects.get(branch='MAIN', product_id=1301).tax_rate), '7.50')
		shared = self.client.get('/api/products/shared/')
		self.assertEqual(shared.status_code, 200)
		product = next(item for item in shared.json()['products'] if item['product_id'] == 1301)
		self.assertEqual(product['tax_rate'], '7.50')

	def test_branch_price_update_refreshes_shared_catalog_price(self):
		for branch in ('BranchA', 'BranchB', 'MAIN'):
			ProductCatalog.objects.create(
				branch=branch, product_id=1201, product_name='Priced Product', selling_price='10.00',
			)

		queued = self.client.post(
			'/api/stock/prices/',
			data=json.dumps({
				'branch': 'BranchA', 'product_id': 1201,
				'product_name': 'Priced Product', 'selling_price': '12.50',
			}),
			content_type='application/json',
		)

		self.assertEqual(queued.status_code, 202)
		self.assertEqual(str(ProductCatalog.objects.get(branch='MAIN', product_id=1201).selling_price), '12.50')
		shared = self.client.get('/api/products/shared/')
		self.assertEqual(shared.status_code, 200)
		product = next(item for item in shared.json()['products'] if item['product_id'] == 1201)
		self.assertEqual(product['selling_price'], '12.50')

	def test_branch_list_excludes_catalog_branch_without_heartbeat(self):
		ProductCatalog.objects.create(
			branch='Offline Branch', product_id=1000, product_name='Offline Product',
		)

		response = self.client.get('/api/branches/')

		self.assertEqual(response.status_code, 200)
		self.assertNotIn('Offline Branch', [item['name'] for item in response.json()['branches']])

	def test_branch_list_accepts_only_online_branch_pc_heartbeats(self):
		main_response = self.client.post(
			'/api/branches/',
			data=json.dumps({'branch': 'CloudPOS', 'device_role': 'Main PC'}),
			content_type='application/json',
		)
		branch_response = self.client.post(
			'/api/branches/',
			data=json.dumps({'branch': 'Online Branch', 'device_role': 'Branch PC'}),
			content_type='application/json',
		)

		self.assertEqual(main_response.status_code, 400)
		self.assertEqual(branch_response.status_code, 200)
		branches = self.client.get('/api/branches/').json()['branches']
		self.assertEqual([item['name'] for item in branches], ['Online Branch'])

	def test_branch_list_supports_twelve_online_branch_pc_heartbeats(self):
		for index in range(1, 13):
			response = self.client.post(
				'/api/branches/',
				data=json.dumps({'branch': f'Branch {index}', 'device_role': 'Branch PC'}),
				content_type='application/json',
			)
			self.assertEqual(response.status_code, 200)

		branches = self.client.get('/api/branches/').json()['branches']
		self.assertEqual(len(branches), 12)

	def test_publish_product_catalog_upserts_products(self):
		response = self.client.post(
			'/api/products/publish/',
			data=json.dumps({'products': [{
				'branch': 'BranchA', 'product_id': 1002, 'product_name': 'Published Product',
				'available_quantity': 8, 'selling_price': '12.50', 'tax_rate': '5',
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['updated'], 1)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=1002)
		self.assertEqual(product.product_name, 'Published Product')
		self.assertEqual(product.available_quantity, 8)
		self.assertEqual(str(product.tax_rate), '5.00')

	def test_web_can_queue_branch_product_and_branch_can_acknowledge_it(self):
		BranchHeartbeat.objects.create(branch='BranchB', last_seen=timezone.now())
		BranchHeartbeat.objects.create(branch='Offline Branch', last_seen=timezone.now() - timedelta(days=1))
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({
				'branch': 'BranchA', 'product_name': 'New Branch Product',
				'product_id': '12100001', 'product_code': 'NEW-001', 'barcode': '990001', 'initial_quantity': '8', 'selling_price': '4.25', 'tax_rate': '7.50',
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(set(response.json()['branches']), {'BranchA', 'BranchB', 'Offline Branch'})
		product_id = response.json()['product_id']
		self.assertEqual(product_id, 12_100_001)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).product_code, '12100002')
		self.assertTrue(ProductCatalog.objects.get(branch='BranchB', product_id=product_id).pending_product_creation)
		self.assertTrue(ProductCatalog.objects.get(branch='Offline Branch', product_id=product_id).pending_product_creation)
		self.assertFalse(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).branch_confirmed)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).pending_stock_quantity, 0)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).tax_rate), '7.50')
		not_visible = self.client.get('/api/products/?branch=BranchA&q=New%20Branch%20Product')
		self.assertEqual(not_visible.status_code, 200)
		self.assertEqual(not_visible.json()['products'], [])

		poll = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(poll.status_code, 200)
		self.assertEqual(poll.json()['pending_product_creations'][0]['product_name'], 'New Branch Product')
		self.assertEqual(poll.json()['pending_product_creations'][0]['initial_quantity'], '0')
		self.assertEqual(poll.json()['pending_product_creations'][0]['tax_rate'], '7.50')

		complete = self.client.post(
			'/api/products/create/complete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': product_id, 'actual_product_id': 2001, 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(complete.status_code, 200)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=2001)
		self.assertFalse(product.pending_product_creation)
		self.assertTrue(product.branch_confirmed)
		self.assertEqual(str(product.tax_rate), '7.50')
		self.assertEqual(str(ProductCatalog.objects.get(branch='MAIN', product_id=2001).tax_rate), '7.50')
		visible = self.client.get('/api/products/?branch=BranchA&q=New%20Branch%20Product')
		self.assertEqual([item['product_id'] for item in visible.json()['products']], [2001])
		main_products = self.client.get('/api/products/?branch=MAIN&q=New%20Branch%20Product')
		self.assertEqual([item['product_id'] for item in main_products.json()['products']], [2001])
		transfer = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 2001, 'product_name': 'New Branch Product', 'quantity': 6}),
			content_type='application/json',
		)
		self.assertEqual(transfer.status_code, 202)
		self.assertEqual(MainStockBalance.objects.get(product_id=2001).quantity, 6)

	def test_web_rejects_main_as_branch_product_target(self):
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({'branch': 'MAIN', 'product_name': 'Invalid Product', 'selling_price': '1.00'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)

	def test_web_rejects_product_id_above_sql_integer_limit(self):
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({
				'branch': 'BranchA', 'product_id': '12100000324',
				'product_name': 'TEST FOR ALL BRANCHIES', 'product_code': '12100000325',
				'initial_quantity': '0', 'selling_price': '1',
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Product ID cannot exceed 2147483647.')
		self.assertFalse(ProductCatalog.objects.filter(product_id=12100000324).exists())

	def test_web_rejects_duplicate_product_id_with_clear_message(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=12100002, product_name='Existing Product')
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({'branch': 'BranchB', 'product_id': 12100002, 'product_name': 'Duplicate Product', 'selling_price': '1.00'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 409)
		self.assertIn('already queued', response.json()['message'])

	def test_web_can_queue_and_branch_can_claim_invoice_reprint(self):
		response = self.client.post(
			'/api/invoice-reprint/',
			data=json.dumps({'branch': 'BranchA', 'invoice': 'INV-100'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		poll = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(poll.status_code, 200)
		self.assertEqual(poll.json()['pending_invoice_reprints'][0]['invoice'], 'INV-100')
		request_id = poll.json()['pending_invoice_reprints'][0]['request_id']
		complete = self.client.post(
			'/api/invoice-reprint/complete/',
			data=json.dumps({'request_id': request_id, 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(complete.status_code, 200)
		self.assertEqual(InvoiceReprintRequest.objects.get(request_id=request_id).status, 'completed')

	def test_product_search_matches_code_barcode_and_id(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1001, product_name='Search Product',
			product_code='CODE-1001', barcode='BAR-1001',
		)

		for query in ('CODE-1001', 'BAR-1001', '1001'):
			response = self.client.get(f'/api/products/?branch=BranchA&q={query}')
			self.assertEqual(response.status_code, 200)
			self.assertEqual([item['product_id'] for item in response.json()['products']], [1001])

	def test_stock_summary_sold_and_received_are_daily_for_selected_branch(self):
		from django.utils import timezone
		from datetime import timedelta

		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10, sold_quantity=99,
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=5, source='today',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=3, source='today',
		)
		old_movement = StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=40, source='yesterday',
		)
		StockMovement.objects.filter(pk=old_movement.pk).update(
			created_at=timezone.now() - timedelta(days=1),
		)
		StockMovement.objects.create(
			branch='BranchB', product_id=999, product_name='Test Product',
			movement_type='received', quantity=70, source='other branch',
		)

		response = self.client.get('/api/stock/summary/?branch=BranchA')

		self.assertEqual(response.status_code, 200)
		product = response.json()['products'][0]
		self.assertEqual(product['received_quantity'], '5')
		self.assertEqual(product['sold_quantity'], '3')

	def test_stock_movements_can_be_filtered_by_date(self):
		from django.utils import timezone
		from datetime import timedelta

		today = StockMovement.objects.create(
			branch='BranchA', product_id=1001, product_name='Today Product',
			movement_type='received', quantity=1, source='sync',
		)
		yesterday = StockMovement.objects.create(
			branch='BranchA', product_id=1002, product_name='Yesterday Product',
			movement_type='received', quantity=2, source='sync',
		)
		StockMovement.objects.filter(pk=yesterday.pk).update(
			created_at=timezone.now() - timedelta(days=1),
		)

		response = self.client.get(f'/api/stock/movements/?branch=BranchA&date={timezone.localdate().isoformat()}')

		self.assertEqual(response.status_code, 200)
		self.assertEqual([movement['product_id'] for movement in response.json()['movements']], [today.product_id])

	def test_stock_movements_reject_invalid_timezone_offset(self):
		response = self.client.get('/api/stock/movements/?branch=BranchA&date=2026-09-25&timezone_offset=900')

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Timezone offset is out of range.')

	def test_stock_movements_returns_only_twenty_newest_rows(self):
		for product_id in range(1, 26):
			StockMovement.objects.create(
				branch='BranchA', product_id=product_id, product_name=f'Product {product_id}',
				movement_type='received', quantity=1, source='sync',
			)

		response = self.client.get('/api/stock/movements/?branch=BranchA')

		self.assertEqual(response.status_code, 200)
		movements = response.json()['movements']
		self.assertEqual(len(movements), 20)

	def test_catalog_sync_records_reduction_as_sold(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)

		decrease = self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'BranchA', 'entered_by': 'branch-user', 'products': [{
				'product_id': 999, 'product_name': 'Test Product', 'available_quantity': 7,
			}]}),
			content_type='application/json',
		)
		increase = self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'BranchA', 'entered_by': 'branch-user', 'products': [{
				'product_id': 999, 'product_name': 'Test Product', 'available_quantity': 12,
			}]}),
			content_type='application/json',
		)

		self.assertEqual(decrease.status_code, 200)
		self.assertEqual(increase.status_code, 200)
		self.assertEqual(StockMovement.objects.filter(product_id=999, movement_type='sold').count(), 1)
		self.assertEqual(StockMovement.objects.get(product_id=999, movement_type='sold').quantity, 3)

	def test_catalog_sync_does_not_sell_stock_take_reduction(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': 100, 'branch': 'BranchA'}),
			content_type='application/json',
		)
		response = self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'BranchA', 'entered_by': 'branch-user', 'products': [{
				'product_id': 999, 'product_name': 'Test Product', 'available_quantity': 20,
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(StockMovement.objects.filter(product_id=999, movement_type='sold').exists())
		catalog = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		self.assertEqual(catalog.available_quantity, 20)
		self.assertFalse(catalog.pending_stock_adjustment)

	def test_catalog_sync_does_not_duplicate_web_transfer_movement(self):
		ProductCatalog.objects.create(
			branch='Mini Market', product_id=4941, product_name='LOBELS BREAD 700G #B', available_quantity=0,
		)
		StockMovement.objects.create(
			branch='Mini Market', product_id=4941, product_name='LOBELS BREAD 700G #B',
			movement_type='received', quantity=200, source='Admin (transfer to Mini Market)',
		)

		response = self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'Mini Market', 'entered_by': 'HP EliteBook', 'products': [{
				'product_id': 4941, 'product_name': 'LOBELS BREAD 700G #B', 'available_quantity': 200,
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(StockMovement.objects.filter(product_id=4941, movement_type='received').count(), 1)

	def test_product_movement_history_filters_product_and_date(self):
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=12, source='transfer-admin',
		)

		response = self.client.get('/api/stock/movements/history/?branch=BranchA&product=999&from=2020-01-01&to=2099-12-31')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.json()['movements']), 1)
		self.assertEqual(response.json()['movements'][0]['source'], 'transfer-admin')
		self.assertEqual(response.json()['movements'][0]['balance'], '12')

		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=2, source='HP EliteBook',
		)
		sold_response = self.client.get('/api/stock/movements/history/?branch=BranchA&product=999&from=2020-01-01&to=2099-12-31')
		sold_movement = next(item for item in sold_response.json()['movements'] if item['movement_type'] == 'sold')
		self.assertEqual(sold_movement['source'], 'Sold')

	def test_product_movement_history_adjustment_sets_balance(self):
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=12, source='transfer-admin',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='adjusted', quantity=5, source='Admin (stock take)',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=3, source='transfer-admin',
		)

		response = self.client.get('/api/stock/movements/history/?branch=BranchA&product=999&from=2020-01-01&to=2099-12-31')

		self.assertEqual(response.status_code, 200)
		self.assertEqual([item['balance'] for item in response.json()['movements']], ['8', '5', '12'])

	def test_product_movement_history_requires_branch(self):
		response = self.client.get('/api/stock/movements/history/')

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Select a branch first.')

	def test_product_movement_history_requires_product(self):
		response = self.client.get('/api/stock/movements/history/?branch=BranchA')

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Select a product first.')


@override_settings(STORAGES={
	'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
	'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class DeletionQueueTests(TestCase):
	def setUp(self):
		self.client.force_login(get_user_model().objects.create_user(
			username='operator', password='OperatorPass4182!'
		))

	def test_dashboard_counts_saved_pending_record(self):
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-100', 'branch': 'Branch A'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(self.client.get('/api/main-sync/').json()['pending_count'], 1)

	def test_dashboard_and_queue_are_not_cached(self):
		self.assertEqual(self.client.get('/').headers['Cache-Control'], 'no-store, no-cache, must-revalidate, max-age=0')
		self.assertEqual(self.client.get('/api/main-sync/').headers['Cache-Control'], 'no-store, no-cache, must-revalidate, max-age=0')

	def test_confirmation_moves_record_to_processed_count(self):
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-200', 'branch': 'Branch A'}),
			content_type='application/json',
		)
		deletion_id = response.json()['deletion_id']

		response = self.client.post(
			'/api/confirm-deletion/',
			data=json.dumps({
				'deletion_id': deletion_id,
				'deleted_rows': 1,
				'branch': 'Branch A',
				'success': True,
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		summary = self.client.get('/api/main-sync/').json()
		self.assertEqual(summary['pending_count'], 0)
		self.assertEqual(summary['processed_count'], 1)

	def test_main_queue_includes_cancellation_and_sales_report_statuses(self):
		cancellation = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-QUEUE', 'branch': 'Branch A'}),
			content_type='application/json',
		).json()
		report = self.client.post(
			'/api/sales-report/',
			data=json.dumps({'report_date': '2026-09-25', 'branches': ['Branch A']}),
			content_type='application/json',
		).json()

		queue = self.client.get('/api/main-sync/').json()['queue']
		statuses = {(item['type'], item['id']): item['status'] for item in queue}
		self.assertEqual(statuses[('cancellation', cancellation['deletion_id'])], 'pending')
		self.assertEqual(statuses[('sales_report', report['reports'][0]['id'])], 'pending')

	def test_queue_shows_sent_and_printed_states(self):
		cancellation_id = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-SENT', 'branch': 'Branch A'}),
			content_type='application/json',
		).json()['deletion_id']
		report_id = self.client.post(
			'/api/sales-report/',
			data=json.dumps({'report_date': '2026-09-25', 'branches': ['Branch A']}),
			content_type='application/json',
		).json()['reports'][0]['id']

		self.client.get('/api/branch-sync/?branch=Branch A')
		self.client.post(
			'/api/sales-report/complete/',
			data=json.dumps({'report_id': report_id, 'success': True, 'row_count': 4, 'branch': 'Branch A'}),
			content_type='application/json',
		)

		queue = self.client.get('/api/main-sync/').json()['queue']
		statuses = {item['id']: item['status'] for item in queue}
		self.assertEqual(statuses[cancellation_id], 'processing')
		self.assertEqual(statuses[report_id], 'printed')

	def test_scheduled_report_is_only_released_to_selected_branch_when_due(self):
		scheduled_at = timezone.now() + timedelta(minutes=2)
		response = self.client.post(
			'/api/sales-report/',
			data=json.dumps({
				'report_date': '2026-09-25',
				'branches': ['Branch A'],
				'scheduled_at': scheduled_at.isoformat(),
			}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		report_id = response.json()['reports'][0]['id']
		self.assertEqual(SalesReportRequest.objects.get(pk=report_id).status, 'scheduled')
		self.assertEqual(self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports'], [])

		SalesReportRequest.objects.filter(pk=report_id).update(scheduled_at=timezone.now() - timedelta(seconds=1))
		self.assertEqual(self.client.get('/api/branch-sync/?branch=Branch B').json()['pending_reports'], [])
		branch_a_reports = self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports']
		self.assertEqual([report['id'] for report in branch_a_reports], [report_id])

	def test_daily_report_schedules_are_branch_specific_and_repeat_once_per_day(self):
		fixed_now = datetime(2026, 9, 26, 12, 1, tzinfo=datetime_timezone.utc)
		response = self.client.post(
			'/api/sales-report/schedules/',
			data=json.dumps({
				'schedules': [
					{'branch': 'Branch A', 'time': '12:00', 'timezone': 'UTC'},
					{'branch': 'Branch B', 'time': '12:02', 'timezone': 'UTC'},
				],
			}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(SalesReportSchedule.objects.count(), 2)

		with patch('loraApi.views.timezone.now', return_value=fixed_now):
			main_queue = self.client.get('/api/main-sync/').json()['queue']
			daily_reports = [item for item in main_queue if item['type'] == 'sales_report']
			self.assertEqual(len(daily_reports), 1)
			self.assertEqual(daily_reports[0]['branch'], 'Branch A')
			branch_b_reports = self.client.get('/api/branch-sync/?branch=Branch B').json()['pending_reports']
			self.assertEqual(branch_b_reports, [])
			branch_a_reports = self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports']
			self.assertEqual(len(branch_a_reports), 1)
			self.assertEqual(branch_a_reports[0]['report_date'], '2026-09-26')
			self.assertEqual(self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports'], [])
			main_queue = self.client.get('/api/main-sync/').json()['queue']
			daily_reports = [item for item in main_queue if item['type'] == 'sales_report']
			self.assertEqual(len(daily_reports), 1)
			self.assertEqual(SalesReportRequest.objects.filter(branch='Branch A', report_date=fixed_now.date()).count(), 1)

		with patch('loraApi.views.timezone.now', return_value=fixed_now + timedelta(days=1)):
			main_queue = self.client.get('/api/main-sync/').json()['queue']
			next_day_reports = [
				item for item in main_queue
				if item['type'] == 'sales_report' and item['branch'] == 'Branch A' and item['detail'] == '2026-09-27'
			]
		self.assertEqual(len(next_day_reports), 1)
		self.assertEqual(next_day_reports[0]['detail'], '2026-09-27')

	def test_history_returns_confirmed_invoices_newest_first(self):
		older = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-OLD', 'branch': 'Branch A'}),
			content_type='application/json',
		).json()['deletion_id']
		newer = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-NEW', 'branch': 'Branch B'}),
			content_type='application/json',
		).json()['deletion_id']

		for deletion_id, branch in [(older, 'Branch A'), (newer, 'Branch B')]:
			self.client.post(
				'/api/confirm-deletion/',
				data=json.dumps({'deletion_id': deletion_id, 'deleted_rows': 1, 'branch': branch}),
				content_type='application/json',
			)

		response = self.client.get('/api/cancellation-history/?branch=Branch')
		self.assertEqual(response.status_code, 200)
		self.assertEqual([item['invoice'] for item in response.json()['cancellations']], ['INV-NEW', 'INV-OLD'])

	def test_history_filters_by_invoice_and_returns_deleted_by(self):
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-FILTER', 'branch': 'Branch C'}),
			content_type='application/json',
		)
		deletion_id = response.json()['deletion_id']
		self.client.post(
			'/api/confirm-deletion/',
			data=json.dumps({
				'deletion_id': deletion_id,
				'deleted_rows': 2,
				'branch': 'Branch C',
				'deleted_by': 'cashier-17',
			}),
			content_type='application/json',
		)

		response = self.client.get('/api/cancellation-history/?invoice=FILTER')
		self.assertEqual(response.json()['cancellations'][0]['deleted_by'], 'operator')


@override_settings(STORAGES={
	'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
	'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class AuthenticationTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser(
			username='Admin', password='Tash1nga4182', email='admin@example.com'
		)

	def test_dashboard_requires_login(self):
		response = self.client.get('/')
		self.assertRedirects(response, '/login/?next=/')

	def test_admin_can_create_user_and_change_password(self):
		self.client.force_login(self.admin)
		response = self.client.post('/users/', {
			'action': 'create',
			'username': 'operator',
			'password1': 'OperatorPass4182!',
			'password2': 'OperatorPass4182!',
		})
		self.assertEqual(response.status_code, 200)
		operator = get_user_model().objects.get(username='operator')
		self.assertFalse(operator.is_superuser)

		response = self.client.post('/users/', {
			'action': 'change_password',
			'user_id': operator.pk,
			'new_password1': 'ChangedPass4182!',
			'new_password2': 'ChangedPass4182!',
		})
		self.assertEqual(response.status_code, 200)
		self.assertTrue(operator.__class__.objects.get(pk=operator.pk).check_password('ChangedPass4182!'))

		response = self.client.post('/users/', {'action': 'delete', 'user_id': operator.pk})
		self.assertEqual(response.status_code, 200)
		self.assertFalse(get_user_model().objects.filter(username='operator').exists())

	def test_web_cancellation_records_logged_in_username(self):
		self.client.force_login(self.admin)
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-ACTOR', 'branch': 'Branch A'}),
			content_type='application/json',
		)
		from loraApi.models import DeletionRecord
		record = DeletionRecord.objects.get(deletion_id=response.json()['deletion_id'])
		self.assertEqual(record.deleted_by, 'Admin')

	def test_invalid_user_creation_shows_validation_reason(self):
		self.client.force_login(self.admin)
		response = self.client.post('/users/', {
			'action': 'create',
			'username': 'Admin',
			'password1': 'short',
			'password2': 'short',
		})
		self.assertContains(response, 'already exists')

	def test_state_restore_keeps_admin_credentials_and_admin_role(self):
		self.admin.is_superuser = False
		self.admin.is_staff = True
		self.admin.save(update_fields=['is_superuser', 'is_staff'])

		call_command('restore_seed_state')

		admin = get_user_model().objects.get(username='Admin')
		self.assertTrue(admin.check_password('@dm1n4182'))
		self.assertTrue(admin.is_superuser)
		self.assertIsNotNone(authenticate(username='Admin', password='@dm1n4182'))

	def test_admin_can_create_user_and_persist_to_state_file(self):
		self.client.force_login(self.admin)
		response = self.client.post('/users/', {
			'action': 'create',
			'username': 'cashier18',
			'password1': 'Cashier@Pass4182!',
			'password2': 'Cashier@Pass4182!',
		})
		self.assertEqual(response.status_code, 200)
		state = load_state()
		self.assertIn('cashier18', [entry['username'] for entry in state['users']])
		self.assertTrue(get_user_model().objects.get(username='cashier18').check_password('Cashier@Pass4182!'))

	def test_logout_requires_post_and_ends_session(self):
		self.client.force_login(self.admin)
		self.assertEqual(self.client.post('/logout/').status_code, 302)
		self.assertEqual(self.client.post('/logout/').url, '/login/')
		self.assertRedirects(self.client.get('/'), '/login/?next=/')
