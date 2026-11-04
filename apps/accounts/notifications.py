from django.db.models import F
from django.urls import reverse

from apps.accounts.models import Role
from apps.products.models import Product
from apps.sales.models import Payment, PaymentMethod, PaymentStatus, Sale, SaleStatus


def dashboard_notifications(request):
    if request.user.role not in Role.values:
        return {}

    is_manager = request.user.role in (Role.OWNER, Role.SECRETARY)
    low_stock_count = Product.objects.filter(
        is_active=True, stock_quantity__lte=F("minimum_stock_level")
    ).count()
    items = [{
        "label": "Low-stock products",
        "count": low_stock_count,
        "url": f"{reverse('dashboard')}#low-stock",
        "icon": "bi-box-seam",
    }]
    pending_credit_count = None
    if is_manager:
        pending_credit_count = Sale.objects.filter(status=SaleStatus.PENDING_APPROVAL).count()
        items.append({
            "label": "Credit sales awaiting approval",
            "count": pending_credit_count,
            "url": f"{reverse('sales:history')}?payment_status={SaleStatus.PENDING_APPROVAL}",
            "icon": "bi-hourglass-split",
        })

    pending_momo_query = Payment.objects.filter(
        method=PaymentMethod.MTN_MOMO,
        status=PaymentStatus.PENDING_CONFIRMATION,
        sale__status=SaleStatus.APPROVED,
    )
    if not is_manager:
        pending_momo_query = pending_momo_query.filter(sale__created_by=request.user)
    pending_momo_count = pending_momo_query.count()
    items.append({
        "label": "MoMo confirmations pending",
        "count": pending_momo_count,
        "url": reverse("customers:pending_momo"),
        "icon": "bi-phone",
    })
    return {
        "notification_items": items,
        "notification_total": sum(item["count"] for item in items),
        "pending_credit_count": pending_credit_count,
        "pending_momo_count": pending_momo_count,
    }
