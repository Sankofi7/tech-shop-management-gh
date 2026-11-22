"""URL configuration for the project scaffold."""
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

from apps.accounts.views import dashboard

admin.site.login_template = "accounts/login.html"

urlpatterns = []
urlpatterns.append(path("accounts/login/", LoginView.as_view(template_name="accounts/login.html"), name="login"))
urlpatterns.append(path("accounts/logout/", LogoutView.as_view(), name="logout"))
urlpatterns.append(path("accounts/", include("apps.accounts.urls")))
urlpatterns.append(path("audit/", include("apps.audit.urls")))
urlpatterns.append(path("settings/", include("apps.shop_settings.urls")))
urlpatterns.append(path("", dashboard, name="dashboard"))
urlpatterns.append(path("customers/", include("apps.customers.urls")))
urlpatterns.append(path("products/", include("apps.products.urls")))
urlpatterns.append(path("reports/", include("apps.reports.urls")))
urlpatterns.append(path("sales/", include("apps.sales.urls")))
urlpatterns.append(path("returns/", include("apps.returns.urls")))
urlpatterns.append(path("admin/", admin.site.urls))
