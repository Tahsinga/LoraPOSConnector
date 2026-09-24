from django.test import TestCase
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from loraApi.state_store import load_state
from loraApi.models import InvoiceReprintRequest, MainStockBalance, ProductCatalog, StockMovement, StockTransfer
import json


class StockTransferTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser(
			username='transfer-admin', password='TransferPass4182!'
		)
		self.client.force_login(self.admin)
		MainStockBalance.objects.create(product_id=999, product_name='Test Product', quantity=10)

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

	def test_branch_price_changes_only_after_branch_confirmation(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', selling_price='10.00',
		)
		queued = self.client.post(
			'/api/stock/prices/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'selling_price': '12.50'}),
			content_type='application/json',
		)

		self.assertEqual(queued.status_code, 202)
		catalog = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		self.assertEqual(catalog.selling_price, 10)
		self.assertTrue(catalog.pending_price_update)

		confirmed = self.client.post(
			'/api/stock/prices/complete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(confirmed.status_code, 200)
		catalog.refresh_from_db()
		self.assertEqual(catalog.selling_price, 12.5)
		self.assertFalse(catalog.pending_price_update)

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

	def test_web_can_queue_branch_product_and_branch_can_acknowledge_it(self):
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({
				'branch': 'BranchA', 'product_name': 'New Branch Product',
				'product_id': '12100001', 'product_code': 'NEW-001', 'barcode': '990001', 'initial_quantity': '8', 'selling_price': '4.25',
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		product_id = response.json()['product_id']
		self.assertEqual(product_id, 12_100_001)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).product_code, '12100002')
		self.assertFalse(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).branch_confirmed)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).pending_stock_quantity, 8)
		not_visible = self.client.get('/api/products/?branch=BranchA&q=New%20Branch%20Product')
		self.assertEqual(not_visible.status_code, 200)
		self.assertEqual(not_visible.json()['products'], [])

		poll = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(poll.status_code, 200)
		self.assertEqual(poll.json()['pending_product_creations'][0]['product_name'], 'New Branch Product')
		self.assertEqual(poll.json()['pending_product_creations'][0]['initial_quantity'], '8')

		complete = self.client.post(
			'/api/products/create/complete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': product_id, 'actual_product_id': 2001, 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(complete.status_code, 200)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=2001)
		self.assertFalse(product.pending_product_creation)
		self.assertTrue(product.branch_confirmed)
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
