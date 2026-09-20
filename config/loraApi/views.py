import json
import time

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import SetPasswordForm, UserCreationForm
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt

from .models import DeletionRecord, SalesReportRequest
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
BRANCH_ONLINE_SECONDS = 20


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
                'invoice': record.invoice,
                'branch': record.confirmed_branch or record.branch,
                'cancelled_at': (record.confirmation_timestamp or record.timestamp).isoformat(),
                'deleted_rows': record.deleted_rows,
                'deleted_by': record.deleted_by or 'Not reported',
                'source': record.source,
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


@csrf_exempt
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
def branch_status(request):
    """Register a branch heartbeat or list branches currently online."""
    now = time.time()

    if request.method == 'POST':
        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

        branch = str(payload.get('branch', '')).strip()
        if not branch:
            return JsonResponse({'status': 'error', 'message': 'Branch is required.'}, status=400)

        CONNECTED_BRANCHES[branch.lower()] = {
            'name': branch,
            'last_seen': now,
            'device_role': payload.get('device_role', 'Branch PC'),
        }
        return JsonResponse({'status': 'online', 'branch': branch})

    if request.method == 'GET':
        online = [
            branch for branch in CONNECTED_BRANCHES.values()
            if now - branch['last_seen'] <= BRANCH_ONLINE_SECONDS
        ]
        online.sort(key=lambda branch: branch['name'].lower())
        return JsonResponse({'status': 'ok', 'branches': online, 'count': len(online)})

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

        return JsonResponse({
            'status': 'ok',
            'service': 'branch_sync_trigger',
            'branch_filter': branch_name,
            'pending_deletions': pending,
            'count': len(pending),
            'pending_reports': [report_payload(item) for item in reports],
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

        deletion_record.status = 'processed'
        deletion_record.deleted_rows = deleted_rows
        if deleted_by and not deletion_record.deleted_by:
            deletion_record.deleted_by = deleted_by
        deletion_record.confirmed_branch = branch
        deletion_record.confirmation_timestamp = timezone.now()
        deletion_record.save(update_fields=[
            'status', 'deleted_rows', 'deleted_by', 'confirmed_branch', 'confirmation_timestamp'
        ])

    return JsonResponse({
        'status': 'confirmed',
        'message': f'Deletion confirmed: {deleted_rows} row(s) deleted',
        'deletion_id': deletion_id,
        'branch': branch
    })
