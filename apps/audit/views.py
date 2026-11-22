from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from apps.accounts.models import Role
from apps.accounts.permissions import role_required

from .models import AuditEvent


@login_required
@role_required(Role.OWNER, Role.SECRETARY)
def audit_list(request):
    """Show protected, read-only audit history within the shared app layout."""
    events = AuditEvent.objects.select_related("actor")
    page = Paginator(events, 30).get_page(request.GET.get("page"))
    return render(request, "audit/list.html", {"events": page, "page_title": "Audit logs"})
