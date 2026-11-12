from django.contrib import messages
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Prefetch, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import Role
from apps.accounts.permissions import role_required
from apps.customers.ledger import sale_outstanding_balance
from apps.sales.models import Payment, PaymentStatus, Sale, SaleStatus, SaleType
from apps.shop_settings.models import ShopProfile

from .forms import (
    ExchangeSettlementForm, RefundDisbursementForm, RefundMomoConfirmationForm, RefundMomoRejectionForm, RefundRejectionForm,
    RefundRequestForm, ReturnApprovalForm, ReturnRejectionForm, ReturnRequestForm,
)
from .models import (
    ExchangeLink, RefundDisbursement, RefundDisbursementStatus, RefundRequest,
    RefundRequestStatus, ReturnCase, ReturnItem, ReturnKind, ReturnStatus,
)
from .services import (
    ACTIVE_RETURN_STATUSES, _refund_capacity, approve_refund_request, approve_return_request,
    confirm_refund_momo_disbursement, create_refund_request, create_return_request,
    link_exchange_sale, record_refund_disbursement, reject_refund_momo_disbursement, reject_refund_request, reject_return_request,
)


SHOP_ROLES = (Role.OWNER, Role.SECRETARY, Role.EMPLOYEE)
MANAGER_ROLES = (Role.OWNER, Role.SECRETARY)
PAGE_SIZE = 10


def _visible_returns(user):
    """Return cases visible to a user before request-controlled filters apply."""
    returns = ReturnCase.objects.select_related(
        "sale", "sale__customer", "requested_by", "decided_by"
    )
    if user.role == Role.EMPLOYEE:
        returns = returns.filter(sale__created_by=user)
    return returns


def _visible_sales(user):
    sales = Sale.objects.filter(status=SaleStatus.APPROVED).select_related("customer", "created_by")
    if user.role == Role.EMPLOYEE:
        sales = sales.filter(created_by=user)
    return sales


def _remaining_quantities(sale):
    reserved = {
        row["sale_item_id"]: row["total"]
        for row in ReturnItem.objects.filter(
            sale_item__sale=sale, return_case__status__in=ACTIVE_RETURN_STATUSES,
        ).values("sale_item_id").annotate(total=Sum("quantity"))
    }
    return {item.pk: max(item.quantity - (reserved.get(item.pk) or 0), 0) for item in sale.items.all()}


def _return_deadline(sale):
    profile = ShopProfile.get_solo()
    return timezone.localtime(sale.created_at).date() + timedelta(days=profile.return_window_days)


@login_required
@role_required(*SHOP_ROLES)
def return_list(request):
    returns = _visible_returns(request.user)
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    kind = request.GET.get("kind", "")

    if query:
        returns = returns.filter(
            Q(reference__icontains=query)
            | Q(sale__reference__icontains=query)
            | Q(sale__customer__full_name__icontains=query)
            | Q(sale__customer_name_snapshot__icontains=query)
        )
    if status in ReturnStatus.values:
        returns = returns.filter(status=status)
    if kind in ReturnKind.values:
        returns = returns.filter(kind=kind)

    page_obj = Paginator(returns, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "returns/list.html", {
        "page_title": "Returns & exchanges",
        "returns": page_obj,
        "return_statuses": ReturnStatus.choices,
        "return_kinds": ReturnKind.choices,
        "filters": {"q": query, "status": status, "kind": kind},
    })


@login_required
@role_required(*SHOP_ROLES)
def return_sale_select(request):
    query = request.GET.get("q", "").strip()
    sales = _visible_sales(request.user).prefetch_related("items__product")
    if query:
        sales = sales.filter(
            Q(reference__icontains=query)
            | Q(customer__full_name__icontains=query)
            | Q(customer_name_snapshot__icontains=query)
        )
    profile = ShopProfile.get_solo()
    rows = []
    for sale in sales.order_by("-created_at"):
        deadline = timezone.localtime(sale.created_at).date() + timedelta(days=profile.return_window_days)
        remaining = _remaining_quantities(sale)
        if timezone.localdate() <= deadline and any(remaining.values()):
            rows.append({
                "sale": sale,
                "deadline": deadline,
                "items": [
                    {"item": item, "remaining": remaining[item.pk]}
                    for item in sale.items.all() if remaining[item.pk]
                ],
            })
    page_obj = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "returns/sale_select.html", {
        "page_title": "New return or exchange",
        "sales": page_obj,
        "query": query,
    })


@login_required
@role_required(*SHOP_ROLES)
def return_request_create(request, sale_id):
    sale = get_object_or_404(_visible_sales(request.user).prefetch_related("items__product"), pk=sale_id)
    remaining = _remaining_quantities(sale)
    if not any(remaining.values()):
        raise Http404
    profile = ShopProfile.get_solo()
    deadline = timezone.localtime(sale.created_at).date() + timedelta(days=profile.return_window_days)
    if timezone.localdate() > deadline:
        raise Http404
    form = ReturnRequestForm(request.POST or None, sale=sale, remaining_quantities=remaining)
    if request.method == "POST" and form.is_valid():
        try:
            lines = form.selected_lines()
            if form.errors:
                return render(request, "returns/request_form.html", _request_context(sale, form, remaining, deadline))
            return_case = create_return_request(
                sale=sale, user=request.user, kind=form.cleaned_data["kind"],
                reason=form.cleaned_data["reason"], items=lines,
            )
            messages.success(request, f"{return_case.get_kind_display()} request {return_case.reference} was submitted for approval.")
            return redirect("returns:detail", return_case_id=return_case.pk)
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, ValidationError) and hasattr(exc, "message_dict"):
                for field, errors in exc.message_dict.items():
                    form.add_error(None if field in {"items", "sale", "unit_identifiers"} else field, errors)
            else:
                form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "returns/request_form.html", _request_context(sale, form, remaining, deadline))


def _request_context(sale, form, remaining, deadline):
    return {
        "page_title": "New return or exchange",
        "sale": sale,
        "form": form,
        "remaining": remaining,
        "return_deadline": deadline,
        "sale_items": [
            {
                "item": item,
                "remaining": remaining[item.pk],
                "quantity_field": form[f"quantity_{item.pk}"],
                "unit_field": form[f"units_{item.pk}"] if item.product.track_individual_units else None,
            }
            for item in sale.items.all()
        ],
    }


@login_required
@role_required(*SHOP_ROLES)
def return_detail(request, return_case_id):
    return_case = get_object_or_404(
        _visible_returns(request.user).prefetch_related(
            Prefetch(
                "items",
                queryset=ReturnItem.objects.select_related("sale_item__product").prefetch_related(
                    "returned_units__sale_item_unit__unit"
                ),
            ),
            Prefetch(
                "refund_requests",
                queryset=RefundRequest.objects.select_related("requested_by", "decided_by").prefetch_related("disbursements"),
            ),
        ),
        pk=return_case_id,
    )
    exchange_link = ExchangeLink.objects.filter(return_case=return_case).select_related("replacement_sale").prefetch_related(
        "replacement_sale__items__product", "replacement_sale__items__units__unit"
    ).first()
    return render(request, "returns/detail.html", {
        "page_title": "Return details",
        "return_case": return_case,
        "refund_rows": _refund_rows(return_case, request.user),
        "exchange_link": exchange_link,
        "exchange_summary": _exchange_summary(return_case, exchange_link),
        "can_settle_exchange": _can_settle_exchange(return_case, exchange_link, request.user),
        "is_manager": request.user.role in MANAGER_ROLES,
        "can_decide": request.user.role in MANAGER_ROLES and return_case.requested_by_id != request.user.pk,
        "approval_form": ReturnApprovalForm(return_case=return_case) if (
            return_case.status == ReturnStatus.PENDING_APPROVAL
            and request.user.role in MANAGER_ROLES
            and return_case.requested_by_id != request.user.pk
        ) else None,
        "rejection_form": ReturnRejectionForm() if (
            return_case.status == ReturnStatus.PENDING_APPROVAL
            and request.user.role in MANAGER_ROLES
            and return_case.requested_by_id != request.user.pk
        ) else None,
        "inspection_rows": _inspection_rows(return_case, ReturnApprovalForm(return_case=return_case)) if (
            return_case.status == ReturnStatus.PENDING_APPROVAL
            and request.user.role in MANAGER_ROLES
            and return_case.requested_by_id != request.user.pk
        ) else (),
        "refundable_amount": _refund_capacity(return_case) if return_case.status == ReturnStatus.APPROVED else None,
        "can_request_refund": return_case.status == ReturnStatus.APPROVED and _refund_capacity(return_case) > 0,
    })


def _can_settle_exchange(return_case, exchange_link, user):
    return (
        user.role in MANAGER_ROLES
        and return_case.kind == ReturnKind.EXCHANGE
        and return_case.status == ReturnStatus.APPROVED
        and return_case.sale.customer_id is not None
        and exchange_link is None
        and not RefundRequest.objects.filter(return_case=return_case).exists()
    )


def _exchange_summary(return_case, exchange_link):
    if exchange_link is None:
        return None
    replacement = exchange_link.replacement_sale
    credit_applied = min(return_case.total_value, replacement.total)
    remaining_refundable = _refund_capacity(return_case)
    outstanding = sale_outstanding_balance(replacement)
    if replacement.total == return_case.total_value:
        scenario = "Same value: the return credit settles the replacement sale."
    elif replacement.total > return_case.total_value:
        scenario = "Higher value: return credit was applied; the remaining amount is customer debt."
    else:
        scenario = "Lower value: the unused credit remains eligible for the refund workflow."
    return {
        "replacement": replacement,
        "credit_applied": credit_applied,
        "outstanding": outstanding,
        "remaining_refundable": remaining_refundable,
        "scenario": scenario,
    }


def _eligible_replacement_sales(return_case):
    """A convenience queryset only; the settlement service repeats every rule under locks."""
    active_payment = Payment.objects.filter(sale_id=OuterRef("pk")).exclude(status=PaymentStatus.REJECTED)
    return Sale.objects.filter(
        customer_id=return_case.sale.customer_id,
        sale_type=SaleType.CREDIT,
        status=SaleStatus.APPROVED,
    ).exclude(pk=return_case.sale_id).filter(
        exchange_origin__isnull=True,
    ).annotate(has_active_payment=Exists(active_payment)).filter(
        has_active_payment=False,
    ).select_related("customer", "created_by").order_by("-created_at", "-pk")


def _refund_rows(return_case, user):
    """Prepare display values from the prefetched immutable disbursement history."""
    rows = []
    for refund in return_case.refund_requests.all():
        disbursements = list(refund.disbursements.all())
        confirmed = sum(
            (entry.amount for entry in disbursements if entry.status == RefundDisbursementStatus.CONFIRMED),
            Decimal("0.00"),
        )
        pending = sum(
            (entry.amount for entry in disbursements if entry.status == RefundDisbursementStatus.PENDING_CONFIRMATION),
            Decimal("0.00"),
        )
        remaining = max(refund.amount - confirmed - pending, Decimal("0.00"))
        rows.append({
            "refund": refund,
            "disbursements": disbursements,
            "confirmed_amount": confirmed,
            "pending_amount": pending,
            "remaining_amount": remaining,
            "can_disburse": (
                user.role in MANAGER_ROLES
                and refund.status == RefundRequestStatus.APPROVED
                and remaining > 0
            ),
        })
    return rows


@login_required
@role_required(*SHOP_ROLES)
def refund_request_create(request, return_case_id):
    return_case = get_object_or_404(
        _visible_returns(request.user).select_related("sale", "sale__customer").prefetch_related("items__sale_item__product"),
        pk=return_case_id,
    )
    if return_case.status != ReturnStatus.APPROVED:
        raise Http404
    refundable_amount = _refund_capacity(return_case)
    if refundable_amount <= 0:
        raise Http404
    form = RefundRequestForm(request.POST or None, refundable_amount=refundable_amount)
    if request.method == "POST" and form.is_valid():
        try:
            refund = create_refund_request(return_case=return_case, user=request.user, **form.cleaned_data)
            messages.success(request, f"Refund request {refund.reference} was submitted for approval. No money has been disbursed.")
            return redirect("returns:detail", return_case_id=return_case.pk)
        except (ValidationError, PermissionDenied) as exc:
            _add_service_errors(form, exc)
    return render(request, "returns/refund_request_form.html", {
        "page_title": "Request refund", "return_case": return_case,
        "refundable_amount": refundable_amount, "form": form,
    })


@login_required
@role_required(*MANAGER_ROLES)
@require_POST
def refund_approve(request, refund_request_id):
    refund = get_object_or_404(_visible_refunds(request.user), pk=refund_request_id)
    try:
        approved = approve_refund_request(refund_request=refund, user=request.user)
        messages.success(request, f"Refund request {approved.reference} approved. It is not yet disbursed.")
        return redirect("returns:detail", return_case_id=refund.return_case_id)
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return redirect("returns:detail", return_case_id=refund.return_case_id)


@login_required
@role_required(*MANAGER_ROLES)
@require_POST
def refund_reject(request, refund_request_id):
    refund = get_object_or_404(_visible_refunds(request.user), pk=refund_request_id)
    form = RefundRejectionForm(request.POST)
    if form.is_valid():
        try:
            rejected = reject_refund_request(refund_request=refund, user=request.user, reason=form.cleaned_data["reason"])
            messages.success(request, f"Refund request {rejected.reference} was rejected and retained in history.")
            return redirect("returns:detail", return_case_id=refund.return_case_id)
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.error(request, "Enter a meaningful reason before rejecting this refund request.")
    return redirect("returns:detail", return_case_id=refund.return_case_id)


@login_required
@role_required(*MANAGER_ROLES)
def exchange_settlement(request, return_case_id):
    return_case = get_object_or_404(
        _visible_returns(request.user).select_related("sale", "sale__customer"), pk=return_case_id,
    )
    exchange_link = ExchangeLink.objects.filter(return_case=return_case).first()
    if not _can_settle_exchange(return_case, exchange_link, request.user):
        raise Http404
    replacement_sales = _eligible_replacement_sales(return_case)
    form = ExchangeSettlementForm(request.POST or None, replacement_sales=replacement_sales)
    if request.method == "POST" and form.is_valid():
        try:
            link = link_exchange_sale(
                return_case=return_case, replacement_sale=form.cleaned_data["replacement_sale"], user=request.user,
            )
            messages.success(request, f"Exchange settled with replacement sale {link.replacement_sale.reference}.")
            return redirect("returns:detail", return_case_id=return_case.pk)
        except (ValidationError, PermissionDenied) as exc:
            _add_service_errors(form, exc)
    return render(request, "returns/exchange_settlement_form.html", {
        "page_title": "Settle exchange",
        "return_case": return_case,
        "replacement_sales": replacement_sales,
        "form": form,
    })


@login_required
@role_required(*MANAGER_ROLES)
def refund_disbursement_create(request, refund_request_id):
    refund = get_object_or_404(
        _visible_refunds(request.user).select_related("return_case__sale", "return_case__sale__customer"),
        pk=refund_request_id,
    )
    if refund.status != RefundRequestStatus.APPROVED:
        raise Http404
    reserved = refund.confirmed_disbursed_amount + refund.pending_disbursement_amount
    remaining_amount = max(refund.amount - reserved, Decimal("0.00"))
    if remaining_amount <= 0:
        raise Http404
    form = RefundDisbursementForm(
        request.POST or None, refund_request=refund, remaining_amount=remaining_amount,
    )
    if request.method == "POST" and form.is_valid():
        try:
            disbursement = record_refund_disbursement(
                refund_request=refund, user=request.user, amount=form.cleaned_data["amount"],
                method=refund.method, momo_reference=form.cleaned_data.get("momo_reference", ""),
                idempotency_key=form.cleaned_data.get("idempotency_key"),
            )
            if disbursement.status == RefundDisbursementStatus.CONFIRMED:
                messages.success(request, "Cash refund disbursement was recorded as paid.")
            else:
                messages.success(request, "MTN MoMo refund was recorded and is awaiting independent confirmation.")
            return redirect("returns:detail", return_case_id=refund.return_case_id)
        except (ValidationError, PermissionDenied) as exc:
            _add_service_errors(form, exc)
    return render(request, "returns/refund_disbursement_form.html", {
        "page_title": "Record refund disbursement",
        "refund": refund,
        "return_case": refund.return_case,
        "remaining_amount": remaining_amount,
        "form": form,
    })


@login_required
@role_required(*MANAGER_ROLES)
@require_POST
def refund_momo_confirm(request, disbursement_id):
    disbursement = get_object_or_404(
        RefundDisbursement.objects.select_related("refund_request__return_case"), pk=disbursement_id,
    )
    form = RefundMomoConfirmationForm(request.POST)
    if form.is_valid():
        try:
            confirmed = confirm_refund_momo_disbursement(
                disbursement=disbursement, user=request.user,
                confirmation_reference=form.cleaned_data["confirmation_reference"],
            )
            messages.success(request, f"MTN MoMo refund of GH₵{confirmed.amount:.2f} was confirmed as paid.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.error(request, "Enter a MoMo confirmation reference before confirming this refund.")
    return redirect("returns:detail", return_case_id=disbursement.refund_request.return_case_id)


@login_required
@role_required(*MANAGER_ROLES)
@require_POST
def refund_momo_reject(request, disbursement_id):
    disbursement = get_object_or_404(
        RefundDisbursement.objects.select_related("refund_request__return_case"), pk=disbursement_id,
    )
    form = RefundMomoRejectionForm(request.POST)
    if form.is_valid():
        try:
            rejected = reject_refund_momo_disbursement(
                disbursement=disbursement, user=request.user, reason=form.cleaned_data["reason"],
            )
            messages.success(request, f"MTN MoMo refund of GH₵{rejected.amount:.2f} was rejected and remains in history.")
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.error(request, "Enter a meaningful reason before rejecting this MTN MoMo refund.")
    return redirect("returns:detail", return_case_id=disbursement.refund_request.return_case_id)


def _visible_refunds(user):
    refunds = RefundRequest.objects.select_related("return_case", "return_case__sale", "requested_by", "decided_by")
    if user.role == Role.EMPLOYEE:
        refunds = refunds.filter(return_case__sale__created_by=user)
    return refunds


def _add_service_errors(form, exc):
    if isinstance(exc, ValidationError) and hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))


@login_required
@role_required(*MANAGER_ROLES)
@require_POST
def return_approve(request, return_case_id):
    return_case = get_object_or_404(_visible_returns(request.user).prefetch_related("items__sale_item__product"), pk=return_case_id)
    form = ReturnApprovalForm(request.POST, return_case=return_case)
    if form.is_valid():
        try:
            approved = approve_return_request(
                return_case=return_case, user=request.user, item_decisions=form.item_decisions(),
            )
            messages.success(request, f"Return request {approved.reference} approved. Inventory and financial allocations were recorded.")
            return redirect("returns:detail", return_case_id=return_case.pk)
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.error(request, "Review every inspection condition and inventory disposition before approving.")
    return _render_return_detail(request, return_case, approval_form=form, rejection_form=ReturnRejectionForm())


@login_required
@role_required(*MANAGER_ROLES)
@require_POST
def return_reject(request, return_case_id):
    return_case = get_object_or_404(_visible_returns(request.user), pk=return_case_id)
    form = ReturnRejectionForm(request.POST)
    if form.is_valid():
        try:
            rejected = reject_return_request(return_case=return_case, user=request.user, reason=form.cleaned_data["reason"])
            messages.success(request, f"Return request {rejected.reference} was rejected and retained in history.")
            return redirect("returns:detail", return_case_id=return_case.pk)
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.error(request, "Enter a meaningful reason before rejecting this request.")
    return _render_return_detail(request, return_case, approval_form=ReturnApprovalForm(return_case=return_case), rejection_form=form)


def _render_return_detail(request, return_case, *, approval_form=None, rejection_form=None):
    """Render a failed manager decision with the bound form errors intact."""
    return_case = get_object_or_404(
        _visible_returns(request.user).prefetch_related(
            Prefetch("items", queryset=ReturnItem.objects.select_related("sale_item__product").prefetch_related("returned_units__sale_item_unit__unit")),
            Prefetch("refund_requests", queryset=RefundRequest.objects.select_related("requested_by", "decided_by").prefetch_related("disbursements")),
        ), pk=return_case.pk,
    )
    exchange_link = ExchangeLink.objects.filter(return_case=return_case).select_related("replacement_sale").first()
    return render(request, "returns/detail.html", {
        "page_title": "Return details", "return_case": return_case,
        "refund_rows": _refund_rows(return_case, request.user), "exchange_link": exchange_link,
        "is_manager": request.user.role in MANAGER_ROLES,
        "can_decide": return_case.requested_by_id != request.user.pk,
        "approval_form": approval_form, "rejection_form": rejection_form,
        "inspection_rows": _inspection_rows(return_case, approval_form) if approval_form else (),
    })


def _inspection_rows(return_case, form):
    return [
        {
            "item": item,
            "condition_field": form[f"condition_{item.pk}"],
            "disposition_field": form[f"disposition_{item.pk}"],
        }
        for item in return_case.items.all()
    ]
