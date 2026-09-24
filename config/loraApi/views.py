import json
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from threading import Lock

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import SetPasswordForm, UserCreationForm
from django.db import OperationalError, transaction
from django.db.models import IntegerField, Max, Q, Sum
from django.db.models.functions import Cast
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from .models import DeletionRecord, InvoiceReprintRequest, MainStockBalance, ProductCatalog, SalesReportRequest, StockMovement, StockTransfer
from .state_store import sync_users_to_state

"""
================================================================================
LORA POS RETURNS - DELETION TRIGGER SYSTEM
================================================================================

This API acts as a trigger mediator for invoice deletions across branch and main
.databases. It ensures consistent deletion of the SAME invoice across all systems.

FLOW:
1. Branch cancels invoice locally → Sends complete details to /api/branch-sync/ (POST)
2. Main cancels invoice → Sends complete details to /api/main-sync/ (POST)
3. API stores deletion trigger with all required fields (invoice, product_id, entry_no, branch)
4. Branch polls /api/branch-sync/?branch=X for pending deletions (GET)
5. Branch executes DELETE using exact field values from trigger
6. Branch confirms deletion → Sends to /api/confirm-deletion/ (POST)
7. API tracks completion

KEY: All deletion triggers include COMPLETE data needed for exact WHERE clause match:
- invoice (required)
- product_id (optional but recommended)
- entry_no (optional but recommended)
- branch (required)

This ensures the SAME invoice number that was cancelled is deleted with 100% accuracy.
================================================================================
"""

CONNECTED_BRANCHES = {}
BRANCH_ONLINE_SECONDS = 120
PRODUCT_SYNC_QUEUE = Lock()


def retry_on_database_lock(view_func):
    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        with PRODUCT_SYNC_QUEUE:
            for attempt in range(8):
                try:
                    return view_func(request, *args, **kwargs)
                except OperationalError as error:
                    if 'locked' not in str(error).lower() or attempt == 7:
                        raise
                    time.sleep(0.5 * (attempt + 1))

    return wrapped_view


def whole_quantity(value):
    return value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def cleanup_queues():
    """Keep deletion records available as a permanent cancellation history."""
    return None


def record_payload(record):
    return {
        'id': record.deletion_id,
        'branch': record.branch,
        'invoice': record.invoice,
        'product_id': record.product_id,
        'entry_no': record.entry_no,
        'action': record.action,
        'status': record.status,
        'timestamp': record.timestamp.timestamp(),
        'source': record.source,
        'deleted_from_main': record.deleted_from_main,
        'message': record.message,
        'deleted_rows': record.deleted_rows,
        'confirmed_branch': record.confirmed_branch,
        'confirmation_timestamp': record.confirmation_timestamp.timestamp() if record.confirmation_timestamp else None,
    }


def report_payload(report):
    return {
        'id': report.request_id,
        'branch': report.branch,
        'report_date': report.report_date.isoformat(),
        'status': report.status,
    }


def transfer_payload(transfer):
    return {
        'id': transfer.transfer_id,
        'branch': transfer.branch,
        'product_id': transfer.product_id,
        'product_name': transfer.product_name,
        'quantity': str(transfer.quantity),
		'target_quantity': str(transfer.target_quantity) if transfer.target_quantity is not None else None,
        'status': transfer.status,
        'created_at': transfer.created_at.timestamp(),
    }


def product_payload(product):
    return {
        'branch': product.branch,
        'product_id': product.product_id,
        'product_name': product.product_name,
        'product_code': product.product_code,
        'barcode': product.barcode,
        'available_quantity': str(product.available_quantity),
        'selling_price': str(product.selling_price),
    }


def sum_quantities(queryset):
    return Decimal(queryset.aggregate(total=Sum(Cast('quantity', IntegerField())))['total'] or 0)


def stock_summary_payload(product, branch):
    balance = MainStockBalance.objects.filter(product_id=product.product_id).first()
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    received = sum_quantities(StockMovement.objects.filter(
        product_id=product.product_id,
        branch__iexact=branch,
        movement_type='received',
        created_at__gte=today_start,
        created_at__lt=tomorrow_start,
    ))
    sold = sum_quantities(StockMovement.objects.filter(
        product_id=product.product_id,
        branch__iexact=branch,
        movement_type='sold',
        created_at__gte=today_start,
        created_at__lt=tomorrow_start,
    ))
    sent = sum_quantities(StockTransfer.objects.filter(
        product_id=product.product_id,
        branch__iexact=branch,
        status__in=['pending', 'processing', 'completed'],
    ))
    available = product.available_quantity
    return {
        **product_payload(product),
        'main_quantity': str(balance.quantity if balance else Decimal('0')),
        'received_quantity': str(received),
        'sent_quantity': str(sent),
        'sold_quantity': str(sold),
        'available_quantity': str(available),
    }


@login_required(login_url='/login/')
@csrf_exempt
def index(request):
    """Browser dashboard for managing branch sale cancellations."""
    cleanup_queues()
    return render(request, 'loraApi/dashboard.html', {
        'pending_count': DeletionRecord.objects.filter(status__in=['pending', 'processing']).count(),
        'processed_count': DeletionRecord.objects.filter(status='processed').count(),
    })


@login_required(login_url='/login/')
def main_stock(request):
    """Show the central stock adjustment and branch transfer page."""
    response = render(request, 'loraApi/main_stock.html')
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    return response


@login_required(login_url='/login/')
def product_movement_history(request):
    """Show the searchable, date-filtered stock movement audit page."""
    return render(request, 'loraApi/product_movements.html')


@login_required(login_url='/login/')
@csrf_exempt
def cancellation_history(request):
    """Show the saved cancellation history page."""
    return render(request, 'loraApi/history.html')


@login_required(login_url='/login/')
@csrf_exempt
def cancellation_history_api(request):
    """Return processed cancellations in newest-first order."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch = request.GET.get('branch', '').strip()
    invoice = request.GET.get('invoice', '').strip()
    query = DeletionRecord.objects.filter(status='processed')
    if branch:
        query = query.filter(branch__icontains=branch)
    if invoice:
        query = query.filter(invoice__icontains=invoice)

    records = list(query.order_by('-confirmation_timestamp', '-timestamp')[:500])
    branches = list(
        DeletionRecord.objects.filter(status='processed')
        .exclude(branch='')
        .values_list('branch', flat=True)
        .distinct()
        .order_by('branch')
    )
    return JsonResponse({
        'status': 'ok',
        'count': len(records),
        'branches': branches,
        'cancellations': [
            {
                'id': record.deletion_id,
                'invoice': record.invoice,
                'branch': record.confirmed_branch or record.branch,
                'product_id': record.product_id,
                'entry_no': record.entry_no,
                'cancelled_at': (record.confirmation_timestamp or record.timestamp).isoformat(),
                'deleted_rows': record.deleted_rows,
                'deleted_by': record.deleted_by or 'Not reported',
                'source': record.source,
                'message': record.message,
                'products': json.loads(record.receipt_products or '[]'),
                'total': str(record.receipt_total) if record.receipt_total is not None else '0.00',
            }
            for record in records
        ],
    })


def is_admin(user):
    return user.is_authenticated and user.is_superuser


@user_passes_test(is_admin, login_url='/login/')
def user_management(request):
    User = get_user_model()
    message = ''
    error = ''

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            form = UserCreationForm(request.POST)
            if form.is_valid():
                password = form.cleaned_data['password1']
                user = form.save()
                user.is_staff = True
                user.is_superuser = False
                user.save(update_fields=['is_staff', 'is_superuser'])
                sync_users_to_state({user.username: password})
                message = f'User {user.username} was created.'
            else:
                error = ' '.join(
                    message for messages in form.errors.values() for message in messages
                )
        elif action == 'change_password':
            try:
                user = User.objects.get(pk=request.POST.get('user_id'))
            except User.DoesNotExist:
                error = 'User not found.'
            else:
                form = SetPasswordForm(user, request.POST)
                if form.is_valid():
                    password = form.cleaned_data['new_password1']
                    form.save()
                    sync_users_to_state({user.username: password})
                    if user == request.user:
                        update_session_auth_hash(request, user)
                    message = f'Password changed for {user.username}.'
                else:
                    error = 'Password change failed. Use matching passwords and meet the password rules.'
        elif action == 'delete':
            try:
                user = User.objects.get(pk=request.POST.get('user_id'))
            except User.DoesNotExist:
                error = 'User not found.'
            else:
                if user == request.user:
                    error = 'The active Admin account cannot be deleted.'
                else:
                    username = user.username
                    user.delete()
                    sync_users_to_state()
                    message = f'User {username} was deleted.'

    users = User.objects.order_by('username')
    return render(request, 'loraApi/users.html', {
        'users': users,
        'message': message,
        'error': error,
    })


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def cancel_sale(request):
    """Queue a sale cancellation requested from the browser dashboard."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    invoice = str(payload.get('invoice', '')).strip()
    branch = str(payload.get('branch', '')).strip()
    product_id = payload.get('product_id')

    if not invoice or not branch:
        return JsonResponse({'status': 'error', 'message': 'Invoice and branch are required.'}, status=400)

    deletion_id = f"WEB_{branch}_{invoice}_{int(time.time()*1000)}"
    deletion_record = DeletionRecord.objects.create(
        deletion_id=deletion_id,
        branch=branch,
        invoice=invoice,
        product_id=product_id,
        entry_no='',
        action='cancel_invoice',
        status='pending',
        source='web_dashboard',
        deleted_by=request.user.get_username(),
        message=f'Web cancellation requested for invoice {invoice} at {branch}',
    )

    return JsonResponse({'status': 'accepted', 'message': f'Cancellation queued for invoice {invoice}.', 'deletion_id': deletion_record.deletion_id}, status=202)


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def request_sales_report(request):
    """Queue a Movement sales report for printing on a connected branch PC."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    requested_branches = payload.get('branches')
    if requested_branches is None:
        requested_branches = [payload.get('branch', '')]
    if not isinstance(requested_branches, list):
        requested_branches = [requested_branches]

    branches = [str(branch).strip() for branch in requested_branches if str(branch).strip()]
    if payload.get('all_branches'):
        branches = [branch['name'] for branch in CONNECTED_BRANCHES.values()]
    branches = sorted(set(branches), key=str.casefold)
    report_date = parse_date(str(payload.get('report_date', '')).strip())
    if not branches or report_date is None:
        return JsonResponse({'status': 'error', 'message': 'Select at least one branch and a valid report date.'}, status=400)

    reports = []
    for index, branch in enumerate(branches):
        request_id = f"REPORT_{branch}_{report_date.isoformat()}_{int(time.time() * 1000)}_{index}"
        reports.append(SalesReportRequest.objects.create(
            request_id=request_id,
            branch=branch,
            report_date=report_date,
            requested_by=request.user.get_username(),
        ))
    return JsonResponse({
        'status': 'accepted',
        'message': f'Sales reports queued for {len(reports)} branch(es) on {report_date.isoformat()}.',
        'reports': [report_payload(report) for report in reports],
    }, status=202)


@login_required(login_url='/login/')
@csrf_exempt
def request_invoice_reprint(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)
    try:
        payload = json.loads(request.body or '{}')
        invoice = str(payload.get('invoice', '')).strip()
        branch = str(payload.get('branch', '')).strip()
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    if not invoice or not branch:
        return JsonResponse({'status': 'error', 'message': 'Invoice number and branch are required.'}, status=400)
    request_id = f"REPRINT_{branch}_{invoice}_{int(time.time() * 1000)}"
    reprint = InvoiceReprintRequest.objects.create(
        request_id=request_id,
        branch=branch,
        invoice=invoice,
        requested_by=request.user.get_username(),
    )
    return JsonResponse({'status': 'accepted', 'message': f'Invoice {invoice} reprint queued for {branch}.', 'request_id': reprint.request_id}, status=202)


@csrf_exempt
@retry_on_database_lock
def complete_invoice_reprint(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)
    try:
        payload = json.loads(request.body or '{}')
        request_id = str(payload.get('request_id', '')).strip()
        success = bool(payload.get('success'))
        error = str(payload.get('error', '')).strip()
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    if not request_id:
        return JsonResponse({'status': 'error', 'message': 'Reprint request ID is required.'}, status=400)
    updated = InvoiceReprintRequest.objects.filter(request_id=request_id).update(
        status='completed' if success else 'failed',
        completed_at=timezone.now(),
        error_message=error,
    )
    if not updated:
        return JsonResponse({'status': 'error', 'message': 'Reprint request was not found.'}, status=404)
    return JsonResponse({'status': 'ok', 'success': success})


@login_required(login_url='/login/')
@csrf_exempt
def adjust_main_stock(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        branch = str(payload.get('branch', '')).strip()
        quantity = Decimal(str(payload.get('quantity')))
    except (TypeError, ValueError, InvalidOperation):
        return JsonResponse({'status': 'error', 'message': 'Product ID and quantity are required.'}, status=400)

    if product_id <= 0 or not product_name or not branch or quantity < 0 or quantity != whole_quantity(quantity):
        return JsonResponse({'status': 'error', 'message': 'Product, branch, and a non-negative whole-number stock value are required.'}, status=400)

    for attempt in range(3):
        try:
            with transaction.atomic():
                catalog = ProductCatalog.objects.select_for_update().filter(
                    branch__iexact=branch,
                    product_id=product_id,
                ).first()
                if catalog is None:
                    return JsonResponse({'status': 'error', 'message': 'The product was not found for the selected branch.'}, status=404)
                balance, _ = MainStockBalance.objects.select_for_update().get_or_create(product_id=product_id)
                balance.product_name = product_name
                balance.quantity = quantity
                balance.save(update_fields=['product_name', 'quantity', 'updated_at'])
                StockMovement.objects.create(
                    branch=branch,
                    product_id=product_id,
                    product_name=product_name,
                    movement_type='adjusted',
                    quantity=quantity,
                    source=f'{request.user.get_username()} (stock take)',
                )
                StockTransfer.objects.create(
                    transfer_id=f'STOCKTAKE_{branch}_{product_id}_{int(time.time() * 1000)}',
                    branch=branch,
                    product_id=product_id,
                    product_name=product_name,
                    quantity=quantity,
                    target_quantity=quantity,
                    created_by=request.user.get_username(),
                )
            break
        except OperationalError as error:
            if 'locked' not in str(error).lower() or attempt == 2:
                return JsonResponse({'status': 'error', 'message': 'Stock database is busy. Please retry in a moment.'}, status=503)
            time.sleep(0.2 * (attempt + 1))

    return JsonResponse({'status': 'ok', 'product_id': product_id, 'branch': branch, 'quantity': str(quantity)})


@csrf_exempt
def stock_summary(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch = str(request.GET.get('branch', '')).strip()
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Branch is required.'}, status=400)
    products = list(ProductCatalog.objects.filter(branch__iexact=branch, branch_confirmed=True))
    product_ids = [product.product_id for product in products]
    balance_by_product = dict(MainStockBalance.objects.filter(
        product_id__in=product_ids,
    ).values_list('product_id', 'quantity'))
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    movement_totals = StockMovement.objects.filter(
        branch__iexact=branch,
        product_id__in=product_ids,
        created_at__gte=today_start,
        created_at__lt=tomorrow_start,
    ).values('product_id', 'movement_type').annotate(total=Sum(Cast('quantity', IntegerField())))
    received_by_product = {}
    sold_by_product = {}
    for movement in movement_totals:
        target = received_by_product if movement['movement_type'] == 'received' else sold_by_product
        target[movement['product_id']] = movement['total'] or 0

    sent_by_product = {
        row['product_id']: row['total'] or 0
        for row in StockTransfer.objects.filter(
            product_id__in=product_ids,
            branch__iexact=branch,
            status__in=['pending', 'processing', 'completed'],
        ).values('product_id').annotate(total=Sum(Cast('quantity', IntegerField())))
    }
    summary = []
    for product in products:
        summary.append({
            **product_payload(product),
            'main_quantity': str(balance_by_product.get(product.product_id, Decimal('0'))),
            'received_quantity': str(received_by_product.get(product.product_id, 0)),
            'sent_quantity': str(sent_by_product.get(product.product_id, 0)),
            'sold_quantity': str(sold_by_product.get(product.product_id, 0)),
            'available_quantity': str(product.available_quantity),
        })
    return JsonResponse({'status': 'ok', 'branch': branch, 'products': summary})


@login_required(login_url='/login/')
def stock_movements(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch = str(request.GET.get('branch', '')).strip()
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Select a branch first.'}, status=400)
    movements = StockMovement.objects.filter(branch__iexact=branch).order_by('-created_at', '-id')[:200]
    return JsonResponse({
        'status': 'ok',
        'movements': [
            {
                'product_id': movement.product_id,
                'product_name': movement.product_name,
                'movement_type': movement.movement_type,
                'quantity': str(movement.quantity),
                'source': 'Sold' if movement.movement_type == 'sold' else (movement.source or 'System'),
                'created_at': movement.created_at.isoformat(),
            }
            for movement in movements
        ],
    })


@login_required(login_url='/login/')
def product_movement_history_api(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    product_query = str(request.GET.get('product', '')).strip()
    branch = str(request.GET.get('branch', '')).strip()
    from_date = request.GET.get('from', '').strip()
    to_date = request.GET.get('to', '').strip()
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Select a branch first.'}, status=400)
    if not product_query:
        return JsonResponse({'status': 'error', 'message': 'Select a product first.'}, status=400)

    movements = StockMovement.objects.filter(branch__iexact=branch)
    if product_query:
        product_filters = Q(product_name__icontains=product_query)
        if product_query.isdigit():
            product_filters |= Q(product_id=int(product_query))
        movements = movements.filter(product_filters)

    all_movements = list(movements.order_by('created_at', 'id'))
    product_ids = {movement.product_id for movement in all_movements}
    running_balances = {product_id: Decimal('0') for product_id in product_ids}
    movement_rows = []
    for movement in all_movements:
        if movement.movement_type == 'adjusted':
            running_balances[movement.product_id] = movement.quantity
        elif movement.movement_type == 'sold':
            running_balances[movement.product_id] -= movement.quantity
        else:
            running_balances[movement.product_id] += movement.quantity
        if from_date and movement.created_at.date().isoformat() < from_date:
            continue
        if to_date and movement.created_at.date().isoformat() > to_date:
            continue
        movement_rows.append({
            'product_id': movement.product_id,
            'product_name': movement.product_name,
            'movement_type': movement.movement_type,
            'quantity': str(movement.quantity),
            'balance': str(running_balances[movement.product_id]),
            'branch': movement.branch or 'Main stock',
            'source': 'Sold' if movement.movement_type == 'sold' else (movement.source or 'System'),
            'created_at': movement.created_at.isoformat(),
        })
    movement_rows.reverse()
    movement_rows = movement_rows[:1000]
    return JsonResponse({
        'status': 'ok',
        'movements': movement_rows,
    })


@login_required(login_url='/login/')
def stock_transfer_logs(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    transfers = StockTransfer.objects.order_by('-created_at')[:200]
    return JsonResponse({
        'status': 'ok',
        'transfers': [
            {
                'transfer_id': transfer.transfer_id,
                'branch': transfer.branch,
                'product_id': transfer.product_id,
                'product_name': transfer.product_name,
                'quantity': str(transfer.quantity),
                'status': transfer.status,
                'created_by': transfer.created_by or 'System',
                'created_at': transfer.created_at.isoformat(),
                'completed_at': transfer.completed_at.isoformat() if transfer.completed_at else None,
            }
            for transfer in transfers
        ],
    })


@csrf_exempt
def stock_movement_device_logs(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    movements = StockMovement.objects.order_by('-created_at', '-id')[:2000]
    return JsonResponse({
        'status': 'ok',
        'movements': [
            {
                'id': movement.id,
                'branch': movement.branch,
                'product_id': movement.product_id,
                'product_name': movement.product_name,
                'movement_type': movement.movement_type,
                'quantity': str(movement.quantity),
                'source': movement.source or 'System',
                'created_at': movement.created_at.isoformat(),
            }
            for movement in movements
        ],
    })


@csrf_exempt
def stock_transfer_device_logs(request):
    """Return transfer states for the Windows sync dashboard without claiming work."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    transfers = StockTransfer.objects.order_by('-created_at')[:200]
    return JsonResponse({
        'status': 'ok',
        'transfers': [
            {
                'id': transfer.transfer_id,
                'branch': transfer.branch,
                'product_id': transfer.product_id,
                'product_name': transfer.product_name,
                'quantity': str(transfer.quantity),
                'status': transfer.status,
                'created_at': transfer.created_at.isoformat(),
                'completed_at': transfer.completed_at.isoformat() if transfer.completed_at else None,
            }
            for transfer in transfers
        ],
    })


@csrf_exempt
def cancellation_device_logs(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    cancellations = DeletionRecord.objects.filter(
        status='processed',
        action='cancel_invoice',
    ).order_by('-confirmation_timestamp', '-timestamp')[:2000]
    return JsonResponse({
        'status': 'ok',
        'cancellations': [
            {
                'id': record.deletion_id,
                'invoice': record.invoice,
                'branch': record.branch,
                'product_id': int(record.product_id) if str(record.product_id or '').isdigit() else None,
                'entry_no': record.entry_no,
                'deleted_rows': record.deleted_rows or 0,
                'status': record.status,
                'cancelled_at': (record.confirmation_timestamp or record.timestamp).isoformat(),
                'message': record.message,
            }
            for record in cancellations
        ],
    })


@csrf_exempt
def record_branch_sales(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)
    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        quantity = Decimal(str(payload.get('quantity')))
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch, product ID, product name, and quantity are required.'}, status=400)
    if not branch or product_id <= 0 or not product_name or quantity <= 0 or quantity != whole_quantity(quantity):
        return JsonResponse({'status': 'error', 'message': 'Branch, product, and a positive whole-number quantity are required.'}, status=400)
    movement = StockMovement.objects.create(branch=branch, product_id=product_id, product_name=product_name, movement_type='sold', quantity=quantity, source='branch_sync')
    return JsonResponse({'status': 'ok', 'movement_id': movement.id})


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def create_stock_transfer(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        quantity = Decimal(str(payload.get('quantity')))
    except (TypeError, ValueError, InvalidOperation):
        return JsonResponse({'status': 'error', 'message': 'Branch, product ID, and quantity are required.'}, status=400)

    if not branch or product_id <= 0 or not product_name or quantity <= 0 or quantity != whole_quantity(quantity):
        return JsonResponse({'status': 'error', 'message': 'Branch, product ID, and product name are required; quantity must be a positive whole number.'}, status=400)

    for attempt in range(3):
        try:
            with transaction.atomic():
                balance, _ = MainStockBalance.objects.select_for_update().get_or_create(product_id=product_id)
                balance.product_name = product_name
                balance.quantity += quantity
                balance.save(update_fields=['product_name', 'quantity', 'updated_at'])
                StockMovement.objects.create(
                    branch=branch,
                    product_id=product_id,
                    product_name=product_name,
                    movement_type='received',
                    quantity=quantity,
                    source=f'{request.user.get_username()} (transfer to {branch})',
                )
                transfer = StockTransfer.objects.create(
                    transfer_id=f'TRANSFER_{branch}_{product_id}_{int(time.time() * 1000)}',
                    branch=branch,
                    product_id=product_id,
                    product_name=product_name,
                    quantity=quantity,
                    created_by=request.user.get_username(),
                )
            break
        except OperationalError as error:
            if 'locked' not in str(error).lower() or attempt == 2:
                return JsonResponse({'status': 'error', 'message': 'Stock database is busy. Please retry in a moment.'}, status=503)
            time.sleep(0.2 * (attempt + 1))

    return JsonResponse({'status': 'accepted', 'transfer': transfer_payload(transfer)}, status=202)


@login_required(login_url='/login/')
@csrf_exempt
def request_branch_price_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        selling_price = Decimal(str(payload.get('selling_price')))
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch, product, and a valid price are required.'}, status=400)

    if not branch or product_id <= 0 or not product_name or selling_price < 0:
        return JsonResponse({'status': 'error', 'message': 'Branch, product, and a non-negative price are required.'}, status=400)

    try:
        catalog = ProductCatalog.objects.get(branch__iexact=branch, product_id=product_id)
    except ProductCatalog.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'The product was not found for the selected branch.'}, status=404)

    catalog.product_name = product_name
    catalog.pending_selling_price = selling_price
    catalog.pending_price_update = True
    catalog.save(update_fields=['product_name', 'pending_selling_price', 'pending_price_update', 'updated_at'])
    return JsonResponse({
        'status': 'accepted',
        'branch': catalog.branch,
        'product_id': catalog.product_id,
        'product_name': catalog.product_name,
        'selling_price': str(selling_price),
    }, status=202)


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def create_branch_product(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        requested_product_id = int(payload.get('product_id') or 0)
        product_name = str(payload.get('product_name', '')).strip()
        product_code = str(payload.get('product_code', '')).strip()
        barcode = str(payload.get('barcode', '')).strip()
        initial_quantity = Decimal(str(payload.get('initial_quantity', 0) or 0))
        selling_price = Decimal(str(payload.get('selling_price', 0) or 0))
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch, product name, and a valid price are required.'}, status=400)

    if requested_product_id and requested_product_id < 12_100_000:
        return JsonResponse({'status': 'error', 'message': 'Product ID must be at least 12100000.'}, status=400)
    if not branch or branch.casefold() == 'main' or not product_name or len(product_name) > 250:
        return JsonResponse({'status': 'error', 'message': 'A branch and product name are required.'}, status=400)
    if len(product_code) > 50 or len(barcode) > 100 or initial_quantity < 0 or initial_quantity != whole_quantity(initial_quantity) or selling_price < 0:
        return JsonResponse({'status': 'error', 'message': 'Product code, barcode, quantity, and price are invalid.'}, status=400)

    with transaction.atomic():
        if requested_product_id:
            product_id = requested_product_id
        else:
            maximum_id = ProductCatalog.objects.select_for_update().filter(product_id__gte=12_100_000).aggregate(max_id=Max('product_id'))['max_id']
            product_id = (maximum_id or 12_099_999) + 1
        if ProductCatalog.objects.filter(product_id=product_id).exists():
            return JsonResponse({'status': 'error', 'message': f'Product ID {product_id} is already queued. Enter another unique Product ID.'}, status=409)
        product_code = str(product_id + 1)
        product = ProductCatalog.objects.create(
            branch=branch,
            product_id=product_id,
            product_name=product_name,
            product_code=product_code,
            barcode=barcode,
            pending_stock_quantity=initial_quantity,
            selling_price=selling_price,
            branch_confirmed=False,
            pending_product_creation=True,
        )

    return JsonResponse({
        'status': 'accepted',
        'branch': product.branch,
        'product_id': product.product_id,
        'product_name': product.product_name,
        'product_code': product.product_code,
        'barcode': product.barcode,
        'initial_quantity': str(product.pending_stock_quantity),
        'selling_price': str(product.selling_price),
    }, status=202)


@csrf_exempt
@retry_on_database_lock
def complete_branch_product_creation(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        actual_product_id = int(payload.get('actual_product_id') or product_id)
        success = bool(payload.get('success'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch and product are required.'}, status=400)

    try:
        product = ProductCatalog.objects.get(branch__iexact=branch, product_id=product_id)
    except ProductCatalog.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'The pending product was not found.'}, status=404)

    if success:
        with transaction.atomic():
            product.product_id = actual_product_id
            product.product_code = str(actual_product_id)
            product.branch_confirmed = True
            product.pending_product_creation = False
            product.save(update_fields=['product_id', 'product_code', 'branch_confirmed', 'pending_product_creation', 'updated_at'])
            ProductCatalog.objects.update_or_create(
                branch='MAIN',
                product_id=actual_product_id,
                defaults={
                    'product_name': product.product_name,
                    'product_code': product.product_code,
                    'barcode': product.barcode,
                    'selling_price': product.selling_price,
                    'branch_confirmed': True,
                    'pending_product_creation': False,
                },
            )
            MainStockBalance.objects.get_or_create(
                product_id=actual_product_id,
                defaults={'product_name': product.product_name, 'quantity': 0},
            )
    return JsonResponse({'status': 'ok', 'branch': product.branch, 'product_id': product.product_id, 'success': success})


@csrf_exempt
@retry_on_database_lock
def complete_branch_price_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch and product are required.'}, status=400)

    if not branch or product_id <= 0:
        return JsonResponse({'status': 'error', 'message': 'Branch and product are required.'}, status=400)

    try:
        catalog = ProductCatalog.objects.get(branch__iexact=branch, product_id=product_id)
    except ProductCatalog.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'The product was not found for the selected branch.'}, status=404)

    if payload.get('success'):
        if catalog.pending_price_update and catalog.pending_selling_price is not None:
            catalog.selling_price = catalog.pending_selling_price
            catalog.pending_selling_price = None
            catalog.pending_price_update = False
            catalog.save(update_fields=['selling_price', 'pending_selling_price', 'pending_price_update', 'updated_at'])

    return JsonResponse({'status': 'ok', 'product_id': product_id, 'branch': branch, 'success': bool(payload.get('success'))})


@login_required(login_url='/login/')
def product_catalog(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    query = str(request.GET.get('q', '')).strip()
    branch = str(request.GET.get('branch', '')).strip()
    products = ProductCatalog.objects.filter(branch__iexact=branch or 'MAIN')
    if branch and branch.casefold() != 'main':
        products = products.filter(branch_confirmed=True)
    if query:
        products = products.filter(
            Q(product_name__icontains=query)
            | Q(product_code__icontains=query)
            | Q(barcode__icontains=query)
            | Q(product_id__icontains=query)
        )[:30]
    else:
        products = products[:3000]
    return JsonResponse({'status': 'ok', 'products': [product_payload(product) for product in products]})


@csrf_exempt
@retry_on_database_lock
def sync_product_catalog(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        products = payload.get('products', [])
        branch = str(payload.get('branch', '')).strip()
        entered_by = str(payload.get('entered_by', '')).strip() or f'{branch} branch sync'
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    if not isinstance(products, list) or not branch:
        return JsonResponse({'status': 'error', 'message': 'Branch and products are required.'}, status=400)

    updated = 0
    with transaction.atomic():
        for item in products:
            try:
                product_id = int(item.get('product_id'))
            except (TypeError, ValueError):
                continue
            product_name = str(item.get('product_name', '')).strip()
            if product_id <= 0 or not product_name:
                continue
            available_quantity = whole_quantity(Decimal(str(item.get('available_quantity', 0) or 0)))
            selling_price = Decimal(str(item.get('selling_price', 0) or 0))
            sold_quantity_value = item.get('sold_quantity')
            sold_quantity = whole_quantity(Decimal(str(sold_quantity_value or 0))) if sold_quantity_value is not None else None
            catalog, created = ProductCatalog.objects.select_for_update().get_or_create(
                branch=branch,
                product_id=product_id,
                defaults={'available_quantity': available_quantity, 'selling_price': selling_price, 'branch_confirmed': True},
            )
            catalog.branch_confirmed = True
            previous_quantity = catalog.available_quantity
            previous_sold_quantity = catalog.sold_quantity or Decimal('0')
            stock_take_sale_sync = False
            if catalog.pending_stock_adjustment:
                if catalog.pending_stock_quantity == available_quantity:
                    catalog.pending_stock_adjustment = False
                    catalog.pending_stock_quantity = None
                elif sold_quantity is not None and sold_quantity > previous_sold_quantity:
                    catalog.pending_stock_adjustment = False
                    catalog.pending_stock_quantity = None
                    stock_take_sale_sync = True
                else:
                    available_quantity = catalog.pending_stock_quantity
                    sold_quantity = catalog.sold_quantity
            latest_adjustment = StockMovement.objects.filter(
                branch__iexact=branch,
                product_id=product_id,
                movement_type='adjusted',
            ).order_by('-created_at').first()
            adjustment_sync_pending = latest_adjustment is not None and latest_adjustment.created_at >= catalog.updated_at
            if created and available_quantity > 0:
                transfer_already_logged = StockMovement.objects.filter(
                    branch__iexact=branch,
                    product_id=product_id,
                    movement_type='received',
                    quantity=available_quantity,
                    source__icontains=f'transfer to {branch}',
                ).exists()
                if not transfer_already_logged:
                    StockMovement.objects.create(
                        branch=branch,
                        product_id=product_id,
                        product_name=product_name,
                        movement_type='received',
                        quantity=available_quantity,
                        source=entered_by,
                    )
            elif available_quantity < previous_quantity and (not adjustment_sync_pending or stock_take_sale_sync):
                StockMovement.objects.create(
                    branch=branch,
                    product_id=product_id,
                    product_name=product_name,
                    movement_type='sold',
                    quantity=previous_quantity - available_quantity,
                    source=entered_by,
                )
            elif available_quantity > previous_quantity:
                increase = available_quantity - previous_quantity
                transfer_already_logged = StockMovement.objects.filter(
                    branch__iexact=branch,
                    product_id=product_id,
                    movement_type='received',
                    quantity=increase,
                    source__icontains=f'transfer to {branch}',
                ).exists()
                if not transfer_already_logged:
                    StockMovement.objects.create(
                        branch=branch,
                        product_id=product_id,
                        product_name=product_name,
                        movement_type='received',
                        quantity=increase,
                        source=entered_by,
                    )
            catalog.product_name = product_name
            catalog.product_code = str(item.get('product_code', '')).strip()
            catalog.barcode = str(item.get('barcode', '')).strip()
            catalog.available_quantity = available_quantity
            catalog.selling_price = selling_price
            if sold_quantity is not None:
                catalog.sold_quantity = sold_quantity
            catalog.save()
            updated += 1

    return JsonResponse({'status': 'ok', 'updated': updated})


@csrf_exempt
def product_sync_inbox(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    products = ProductCatalog.objects.exclude(branch__iexact='MAIN').filter(branch_confirmed=True)
    return JsonResponse({'status': 'ok', 'products': [product_payload(product) for product in products]})


@csrf_exempt
def publish_product_catalog(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        products = payload.get('products', [])
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    if not isinstance(products, list):
        return JsonResponse({'status': 'error', 'message': 'Products must be a list.'}, status=400)

    catalog_rows = []
    for item in products:
        try:
            product_id = int(item.get('product_id'))
            available_quantity = Decimal(str(item.get('available_quantity', 0) or 0))
            selling_price = Decimal(str(item.get('selling_price', 0) or 0))
            tax_rate = Decimal(str(item.get('tax_rate', 0) or 0))
        except (TypeError, ValueError, InvalidOperation):
            continue
        product_name = str(item.get('product_name', '')).strip()
        if product_id <= 0 or not product_name:
            continue
        catalog_rows.append(ProductCatalog(
            branch=str(item.get('branch', 'MAIN')).strip() or 'MAIN',
            product_id=product_id,
            product_name=product_name,
            product_code=str(item.get('product_code', '')).strip(),
            barcode=str(item.get('barcode', '')).strip(),
            available_quantity=available_quantity,
            selling_price=selling_price,
            branch_confirmed=True,
            tax_rate=tax_rate,
            pending_price_update=False,
            pending_selling_price=None,
            pending_product_creation=False,
            updated_at=timezone.now(),
        ))

    for attempt in range(5):
        try:
            with transaction.atomic():
                ProductCatalog.objects.bulk_create(
                    catalog_rows,
                    update_conflicts=True,
                    update_fields=[
                        'product_name', 'product_code', 'barcode', 'available_quantity',
                        'selling_price', 'branch_confirmed', 'tax_rate', 'pending_price_update',
                        'pending_selling_price', 'pending_product_creation', 'updated_at',
                    ],
                    unique_fields=['branch', 'product_id'],
                    batch_size=500,
                )
            break
        except OperationalError as error:
            if 'locked' not in str(error).lower() or attempt == 4:
                return JsonResponse({'status': 'error', 'message': 'Product catalog database is busy. Please retry in a moment.'}, status=503)
            time.sleep(0.25 * (attempt + 1))

    return JsonResponse({'status': 'ok', 'updated': len(catalog_rows)})


@csrf_exempt
@retry_on_database_lock
def complete_sales_report(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    request_id = str(payload.get('report_id', '')).strip()
    if not request_id:
        return JsonResponse({'status': 'error', 'message': 'Missing report_id'}, status=400)

    try:
        report = SalesReportRequest.objects.get(request_id=request_id)
    except SalesReportRequest.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Report request not found'}, status=404)

    report.status = 'printed' if payload.get('success') else 'failed'
    report.row_count = int(payload.get('row_count') or 0)
    report.error_message = str(payload.get('error') or '')
    report.completed_at = timezone.now()
    report.save(update_fields=['status', 'row_count', 'error_message', 'completed_at'])
    return JsonResponse({'status': 'ok', 'report': report_payload(report)})


@csrf_exempt
@retry_on_database_lock
def complete_stock_transfer(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    transfer_id = str(payload.get('transfer_id', '')).strip()
    branch = str(payload.get('branch', '')).strip()
    if not transfer_id or not branch:
        return JsonResponse({'status': 'error', 'message': 'Transfer ID and branch are required.'}, status=400)

    try:
        transfer = StockTransfer.objects.get(transfer_id=transfer_id)
    except StockTransfer.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Transfer not found.'}, status=404)

    if not transfer.branch.casefold() == branch.casefold():
        return JsonResponse({'status': 'error', 'message': 'Transfer branch does not match.'}, status=403)

    if payload.get('success'):
        if transfer.status != 'completed':
            with transaction.atomic():
                catalog, created = ProductCatalog.objects.select_for_update().get_or_create(
                    branch=transfer.branch,
                    product_id=transfer.product_id,
                    defaults={
                        'product_name': transfer.product_name,
                        'available_quantity': transfer.quantity,
                    },
                )
                if transfer.target_quantity is not None:
                    catalog.product_name = transfer.product_name or catalog.product_name
                    catalog.available_quantity = transfer.target_quantity
                elif not created and not transfer.branch_stock_applied:
                    catalog.available_quantity += transfer.quantity
                    catalog.product_name = transfer.product_name or catalog.product_name
                if transfer.target_quantity is not None or created or not transfer.branch_stock_applied:
                    catalog.pending_stock_adjustment = True
                    catalog.pending_stock_quantity = catalog.available_quantity
                    catalog.save(update_fields=['product_name', 'available_quantity', 'pending_stock_adjustment', 'pending_stock_quantity', 'updated_at'])
                transfer.status = 'completed'
                transfer.completed_at = transfer.completed_at or timezone.now()
                transfer.branch_stock_applied = True
                transfer.save(update_fields=['status', 'completed_at', 'claimed_at', 'branch_stock_applied'])
    elif transfer.status != 'completed':
        transfer.status = 'pending'
        transfer.claimed_at = None
        transfer.save(update_fields=['status', 'claimed_at'])

    return JsonResponse({'status': 'ok', 'transfer': transfer_payload(transfer)})


@csrf_exempt
def branch_status(request):
    """Register a branch heartbeat or list branches currently online."""
    now = time.time()

    if request.method == 'POST':
        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

        branch = str(payload.get('branch', '')).strip()
        device_role = str(payload.get('device_role', '')).strip()
        if not branch or device_role.casefold() != 'branch pc':
            return JsonResponse({'status': 'error', 'message': 'A saved branch name and Branch PC device role are required.'}, status=400)

        CONNECTED_BRANCHES[branch.lower()] = {
            'name': branch,
            'last_seen': now,
            'device_role': 'Branch PC',
        }
        return JsonResponse({'status': 'online', 'branch': branch})

    if request.method == 'GET':
        online = [
            branch for branch in CONNECTED_BRANCHES.values()
            if now - branch['last_seen'] <= BRANCH_ONLINE_SECONDS
        ]
        branches_by_name = {branch['name'].lower(): {**branch, 'online': True} for branch in online}
        branches = sorted(branches_by_name.values(), key=lambda branch: branch['name'].lower())
        return JsonResponse({'status': 'ok', 'branches': branches, 'count': len(branches), 'online_count': len(online)})

    return JsonResponse({'status': 'error', 'message': 'Use GET or POST method'}, status=405)


@csrf_exempt
def favicon(request):
    """Favicon endpoint - returns empty response"""
    return HttpResponse('', content_type='image/x-icon')


@csrf_exempt
def health_check(request):
    return JsonResponse({
        'status': 'ok',
        'service': 'Lora API Gateway',
        'message': 'Django gateway is running.'
    })


@csrf_exempt
def branch_sync(request):
    """
    TRIGGER SYSTEM FOR BRANCH DELETIONS

    GET: Branch polls for pending deletions
    POST: Branch notifies of deletion completion (from branch cancel)
    """
    cleanup_queues()

    if request.method == 'GET':
        branch_name = request.GET.get('branch', '').strip()
        pending_query = DeletionRecord.objects.filter(status='pending')
        if branch_name:
            pending_query = pending_query.filter(branch__iexact=branch_name)
        pending = [record_payload(item) for item in pending_query]

        report_query = SalesReportRequest.objects.filter(status='pending')
        if branch_name:
            report_query = report_query.filter(branch__iexact=branch_name)
        with transaction.atomic():
            reports = list(report_query.select_for_update())
            for report in reports:
                report.status = 'processing'
                report.save(update_fields=['status'])

        stale_before = timezone.now() - timedelta(minutes=2)
        StockTransfer.objects.filter(
            status='processing',
            created_at__lt=stale_before,
        ).update(status='pending', claimed_at=None)
        transfer_query = StockTransfer.objects.filter(
            status='pending',
        ).filter(
            Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_before),
        ).order_by('created_at')
        if branch_name:
            transfer_query = transfer_query.filter(branch__iexact=branch_name)
        with transaction.atomic():
            transfer = transfer_query.select_for_update().first()
            transfers = [transfer] if transfer else []
            if transfer:
                transfer.claimed_at = timezone.now()
                transfer.save(update_fields=['claimed_at'])

        pending_price_updates = list(ProductCatalog.objects.filter(
            branch__iexact=branch_name,
            pending_price_update=True,
            pending_selling_price__isnull=False,
        ).values('branch', 'product_id', 'product_name', 'pending_selling_price'))
        pending_product_creations = list(ProductCatalog.objects.filter(
            branch__iexact=branch_name,
            pending_product_creation=True,
        ).values('branch', 'product_id', 'product_name', 'product_code', 'barcode', 'pending_stock_quantity', 'selling_price'))
        with transaction.atomic():
            reprint = InvoiceReprintRequest.objects.select_for_update().filter(
                branch__iexact=branch_name,
                status='pending',
            ).order_by('requested_at').first()
            reprints = []
            if reprint:
                reprint.status = 'processing'
                reprint.save(update_fields=['status'])
                reprints.append(reprint)

        return JsonResponse({
            'status': 'ok',
            'service': 'branch_sync_trigger',
            'branch_filter': branch_name,
            'pending_deletions': pending,
            'count': len(pending),
            'pending_reports': [report_payload(item) for item in reports],
            'pending_transfers': [transfer_payload(item) for item in transfers],
            'pending_price_updates': [
                {
                    'branch': item['branch'],
                    'product_id': item['product_id'],
                    'product_name': item['product_name'],
                    'selling_price': str(item['pending_selling_price']),
                }
                for item in pending_price_updates
            ],
            'pending_product_creations': [
                {
                    'branch': item['branch'],
                    'product_id': item['product_id'],
                    'product_name': item['product_name'],
                    'product_code': item['product_code'],
                    'barcode': item['barcode'],
                    'initial_quantity': str(item['pending_stock_quantity'] or 0),
                    'selling_price': str(item['selling_price']),
                }
                for item in pending_product_creations
            ],
            'pending_invoice_reprints': [
                {'request_id': item.request_id, 'branch': item.branch, 'invoice': item.invoice}
                for item in reprints
            ],
            'message': f'Found {len(pending)} deletion(s) to process'
        })

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    invoice = payload.get('invoice')
    product_id = payload.get('product_id')
    entry_no = payload.get('entry_no')
    branch = payload.get('branch', 'unknown')
    deleted = payload.get('deleted', False)

    if not invoice or not deleted:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing required fields: invoice, deleted=true'
        }, status=400)

    deletion_id = f"{branch}_{invoice}_{product_id}_{entry_no}_{int(time.time()*1000)}"
    deletion_record = DeletionRecord.objects.create(
        deletion_id=deletion_id,
        branch=branch,
        invoice=str(invoice),
        product_id=str(product_id) if product_id is not None else None,
        entry_no=str(entry_no) if entry_no is not None else '',
        action=payload.get('action', 'delete'),
        status='pending',
        source='branch_cancel',
        message=f'Branch {branch} cancelled invoice {invoice}',
    )

    return JsonResponse({
        'status': 'accepted',
        'message': f'Deletion request queued for invoice {invoice}',
        'deletion_id': deletion_record.deletion_id,
        'queue_size': DeletionRecord.objects.filter(status='pending').count()
    }, status=202)


@csrf_exempt
def main_sync(request):
    """
    MAIN SYNC ENDPOINT - Main database sends deletions to branches via trigger

    POST: Main requests branch to delete (triggered by cancellation)
    GET: Main checks deletion status
    """
    cleanup_queues()

    if request.method == 'GET':
        pending = [record_payload(item) for item in DeletionRecord.objects.filter(status__in=['pending', 'processing'])]
        processed = [record_payload(item) for item in DeletionRecord.objects.filter(status='processed').order_by('-confirmation_timestamp')[:10]]

        return JsonResponse({
            'status': 'ok',
            'service': 'main_sync_trigger',
            'pending_deletions': pending,
            'pending_count': DeletionRecord.objects.filter(status__in=['pending', 'processing']).count(),
            'recently_processed': processed,
            'processed_count': DeletionRecord.objects.filter(status='processed').count()
        })

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    invoice = payload.get('invoice')
    product_id = payload.get('product_id')
    entry_no = payload.get('entry_no')
    branch = payload.get('branch')
    deleted_from_main = payload.get('deleted_from_main', False)

    if not invoice:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing required field: invoice'
        }, status=400)

    deletion_id = f"MAIN_{branch}_{invoice}_{product_id}_{entry_no}_{int(time.time()*1000)}"
    deletion_record = DeletionRecord.objects.create(
        deletion_id=deletion_id,
        branch=str(branch or ''),
        invoice=str(invoice),
        product_id=str(product_id) if product_id is not None else None,
        entry_no=str(entry_no) if entry_no is not None else '',
        status='pending',
        source='main_cancellation',
        deleted_from_main=deleted_from_main,
        message=f'Main cancelled invoice {invoice} - DELETE FROM ALL BRANCHES',
    )

    return JsonResponse({
        'status': 'triggered',
        'message': f'Deletion trigger issued for invoice {invoice} on branch {branch}',
        'deletion_id': deletion_record.deletion_id,
        'queue_size': DeletionRecord.objects.filter(status='pending').count()
    }, status=202)


@csrf_exempt
@retry_on_database_lock
def confirm_deletion(request):
    """
    CONFIRMATION ENDPOINT - Branch confirms deletion was successful

    POST: Branch sends confirmation that deletion succeeded
    """
    cleanup_queues()

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    deletion_id = payload.get('deletion_id')
    deleted_rows = payload.get('deleted_rows', 0)
    branch = payload.get('branch')
    success = payload.get('success', False)
    deleted_by = str(
        payload.get('deleted_by') or payload.get('username') or payload.get('user_number') or ''
    ).strip()
    receipt_products = payload.get('receipt_products') or []
    receipt_total = payload.get('receipt_total')

    if not deletion_id:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing deletion_id'
        }, status=400)

    with transaction.atomic():
        try:
            deletion_record = DeletionRecord.objects.select_for_update().get(deletion_id=deletion_id)
        except DeletionRecord.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'message': f'Deletion ID {deletion_id} not found in queue'
            }, status=404)

        deletion_record.status = 'processed' if bool(success) and int(deleted_rows or 0) > 0 else 'failed'
        deletion_record.deleted_rows = deleted_rows
        if deleted_by and not deletion_record.deleted_by:
            deletion_record.deleted_by = deleted_by
        deletion_record.confirmed_branch = branch
        deletion_record.confirmation_timestamp = timezone.now()
        if deletion_record.status == 'failed':
            deletion_record.message = f'{deletion_record.message} Branch matched no invoice rows.'
        deletion_record.receipt_products = json.dumps(receipt_products)
        try:
            deletion_record.receipt_total = Decimal(str(receipt_total or '0'))
        except InvalidOperation:
            deletion_record.receipt_total = Decimal('0')
        deletion_record.save(update_fields=[
            'status', 'deleted_rows', 'deleted_by', 'confirmed_branch', 'confirmation_timestamp', 'receipt_products', 'receipt_total'
        ])

    return JsonResponse({
            'status': deletion_record.status,
            'message': f'Deletion {deletion_record.status}: {deleted_rows} row(s) deleted',
        'deletion_id': deletion_id,
        'branch': branch
    })
