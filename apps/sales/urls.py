from django.urls import path

from . import views

app_name = "sales"

urlpatterns = [
    path("", views.sales_history, name="history"),
    path("new/", views.create_sale_view, name="create"),
    path("catalog/", views.product_catalog, name="catalog"),
    path("products/<int:product_id>/units/", views.available_product_units, name="available_units"),
    path("<int:sale_id>/receipt/", views.sale_receipt, name="receipt"),
    path("<int:sale_id>/approve/", views.approve_sale_view, name="approve"),
    path("<int:sale_id>/reject/", views.reject_sale_view, name="reject"),
    path("<int:sale_id>/payments/new/", views.record_payment_view, name="record_payment"),
    path("payments/<int:payment_id>/confirm/", views.confirm_payment_view, name="confirm_payment"),
    path("payments/<int:payment_id>/reject/", views.reject_payment_view, name="reject_payment"),
]
