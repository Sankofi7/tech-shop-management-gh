from django.urls import path

from . import views

app_name = "products"

urlpatterns = [
    path("", views.product_list, name="list"),
    path("new/", views.product_create, name="create"),
    path("<int:product_id>/", views.product_detail, name="detail"),
    path("<int:product_id>/edit/", views.product_edit, name="edit"),
    path("inventory/", views.inventory_overview, name="inventory"),
    path("inventory/receive/", views.stock_receive, name="receive"),
    path("inventory/adjust/", views.stock_adjust, name="adjust"),
    path("inventory/history/", views.movement_history, name="history"),
]
