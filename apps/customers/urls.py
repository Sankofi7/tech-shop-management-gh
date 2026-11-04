from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.customer_list, name="list"),
    path("new/", views.customer_create, name="create"),
    path("debts/", views.debt_list, {"kind": "outstanding"}, name="debts"),
    path("debts/paid/", views.debt_list, {"kind": "paid"}, name="paid_debts"),
    path("debts/pending-momo/", views.debt_list, {"kind": "pending-momo"}, name="pending_momo"),
    path("<int:customer_id>/", views.customer_detail, name="detail"),
    path("<int:customer_id>/edit/", views.customer_edit, name="edit"),
    path("<int:customer_id>/repayments/new/", views.customer_repayment_new, name="repayment_new"),
    path("repayments/<int:payment_id>/receipt/", views.repayment_receipt, name="repayment_receipt"),
]
