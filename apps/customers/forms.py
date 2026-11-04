from decimal import Decimal
import uuid

from django import forms

from apps.sales.models import PaymentMethod, Sale

from .models import Customer, normalise_phone


class CustomerForm(forms.Form):
    full_name = forms.CharField(max_length=200)
    phone_number = forms.CharField(max_length=32, label="Phone number")
    email = forms.EmailField(required=False)
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    is_active = forms.BooleanField(required=False, initial=True, label="Active")

    def __init__(self, *args, customer=None, **kwargs):
        self.customer = customer
        super().__init__(*args, **kwargs)
        if customer:
            self.fields["is_active"].initial = customer.is_active

    def clean_phone_number(self):
        phone = self.cleaned_data["phone_number"]
        key = normalise_phone(phone)
        matches = Customer.objects.filter(normalized_phone=key)
        if self.customer:
            matches = matches.exclude(pk=self.customer.pk)
        if matches.exists():
            raise forms.ValidationError("A customer with this phone number already exists.")
        return phone


class RepaymentForm(forms.Form):
    sale = forms.ModelChoiceField(queryset=Sale.objects.none(), label="Credit sale to repay")
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    method = forms.ChoiceField(choices=PaymentMethod.choices)
    momo_reference = forms.CharField(max_length=120, required=False, label="MTN transaction reference")
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    def __init__(self, *args, sales, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sale"].queryset = sales.select_related("customer", "created_by")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("method") == PaymentMethod.MTN_MOMO and not cleaned.get("momo_reference", "").strip():
            self.add_error("momo_reference", "Enter the MTN transaction reference.")
        if cleaned.get("method") == PaymentMethod.CASH and cleaned.get("momo_reference", "").strip():
            self.add_error("momo_reference", "Cash repayments do not use an MTN reference.")
        return cleaned
