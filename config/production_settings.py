"""Production settings for Render deployment."""

import os

from django.core.exceptions import ImproperlyConfigured
import dj_database_url

from .settings import *


# Required production environment variables.
SECRET_KEY = os.environ.get("SECRET_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
ALLOWED_HOSTS_VALUE = os.environ.get("ALLOWED_HOSTS", "")
CSRF_ORIGINS_VALUE = os.environ.get("CSRF_TRUSTED_ORIGINS", "")

if not SECRET_KEY or len(SECRET_KEY) < 50:
    raise ImproperlyConfigured(
        "Production requires a strong SECRET_KEY of at least 50 characters."
    )

if not DATABASE_URL:
    raise ImproperlyConfigured(
        "Production DATABASE_URL is missing."
    )

if not ALLOWED_HOSTS_VALUE.strip():
    raise ImproperlyConfigured(
        "Production ALLOWED_HOSTS is missing."
    )

if not CSRF_ORIGINS_VALUE.strip():
    raise ImproperlyConfigured(
        "Production CSRF_TRUSTED_ORIGINS is missing."
    )


# Core production settings.
DEBUG = False
ALLOWED_HOSTS = [
    host.strip()
    for host in ALLOWED_HOSTS_VALUE.split(",")
    if host.strip()
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in CSRF_ORIGINS_VALUE.split(",")
    if origin.strip()
]


# Supabase PostgreSQL connection, requiring SSL.
DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
        ssl_require=True,
    )
}


# HTTPS and browser security.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# Enable HSTS only after confirming the hosted HTTPS setup works.
SECURE_HSTS_SECONDS = 0
