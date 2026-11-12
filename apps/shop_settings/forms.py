from django import forms
from django.core.validators import RegexValidator

from .models import ShopProfile


class ShopProfileForm(forms.ModelForm):
    shop_name = forms.CharField(max_length=200, required=True, strip=True)
    contact_phone = forms.CharField(
        max_length=40, required=False, strip=True,
        validators=[RegexValidator(
            regex=r"^[0-9+() .-]*$",
            message="Use digits and common phone formatting characters only.",
        )],
    )
    business_email = forms.EmailField(max_length=254, required=False)
    physical_address = forms.CharField(required=False, strip=True, widget=forms.Textarea(attrs={"rows": 3}))

    class Meta:
        model = ShopProfile
        fields = (
            "shop_name", "contact_phone", "business_email", "physical_address",
            "notify_low_stock", "notify_pending_credit", "notify_pending_momo",
            "return_window_days",
        )
        labels = {
            "notify_low_stock": "Low-stock alerts",
            "notify_pending_credit": "Pending credit-sale approvals",
            "notify_pending_momo": "Pending MTN MoMo confirmations",
            "return_window_days": "Return window (calendar days)",
        }
        help_texts = {
            "notify_low_stock": "Show low-stock alerts on the dashboard and in-app notification menu.",
            "notify_pending_credit": "Show credit sales waiting for owner or secretary approval.",
            "notify_pending_momo": "Show MTN MoMo payments waiting for manual confirmation.",
            "return_window_days": "Number of calendar days from the sale date in which a return request can be submitted.",
        }

    def __init__(self, *args, read_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-control settings-textarea"
            if read_only:
                field.disabled = True
