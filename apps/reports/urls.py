from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.reports_home, name="home"),
    path("sales/", views.sales_report, name="sales"),
    path("payments/", views.payments_report, name="payments"),
    path("credit/", views.credit_report, name="credit"),
    path("inventory/", views.inventory_report, name="inventory"),
    path("employees/", views.employee_report, name="employees"),
]
