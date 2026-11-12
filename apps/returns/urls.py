from django.urls import path

from . import views


app_name = "returns"

urlpatterns = [
    path("", views.return_list, name="list"),
    path("request/", views.return_sale_select, name="request_select"),
    path("request/<int:sale_id>/", views.return_request_create, name="request_create"),
    path("<int:return_case_id>/refunds/new/", views.refund_request_create, name="refund_request_create"),
    path("refunds/<int:refund_request_id>/approve/", views.refund_approve, name="refund_approve"),
    path("refunds/<int:refund_request_id>/reject/", views.refund_reject, name="refund_reject"),
    path("refunds/<int:refund_request_id>/disbursements/new/", views.refund_disbursement_create, name="refund_disbursement_create"),
    path("refund-disbursements/<int:disbursement_id>/confirm/", views.refund_momo_confirm, name="refund_momo_confirm"),
    path("refund-disbursements/<int:disbursement_id>/reject/", views.refund_momo_reject, name="refund_momo_reject"),
    path("<int:return_case_id>/exchange/settle/", views.exchange_settlement, name="exchange_settlement"),
    path("<int:return_case_id>/approve/", views.return_approve, name="approve"),
    path("<int:return_case_id>/reject/", views.return_reject, name="reject"),
    path("<int:return_case_id>/", views.return_detail, name="detail"),
]
