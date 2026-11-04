from django.urls import path

from . import views

app_name = "shop_settings"

urlpatterns = [
    path("", views.settings_page, name="settings"),
]
