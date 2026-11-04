from django import forms
from django.forms import BaseFormSet, formset_factory

from apps.products.models import Product
from apps.customers.models import Customer

from .models import PaymentMethod, SaleType


class ProductChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, product):
        return f"{product.name} ({product.sku}) — GHS {product.selling_price}"


class SaleHeaderForm(forms.Form):
    submission_key = forms.UUIDField(required=False, widget=forms.HiddenInput)
    sale_type = forms.ChoiceField(choices=SaleType.choices, widget=forms.Select(attrs={"class": "form-select"}))
    customer = forms.ModelChoiceField(queryset=Customer.objects.none(), required=False, empty_label="No customer (cash sales only)")
    new_customer_name = forms.CharField(max_length=200, required=False, label="New customer full name")
    new_customer_phone = forms.CharField(max_length=32, required=False, label="New customer phone")
    new_customer_email = forms.EmailField(required=False, label="New customer email (optional)")
    new_customer_address = forms.CharField(required=False, label="New customer address (optional)", widget=forms.Textarea(attrs={"rows": 2}))
    payment_method = forms.ChoiceField(
        choices=PaymentMethod.choices, required=False, widget=forms.Select(attrs={"class": "form-select"})
    )
    payment_amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)
    momo_reference = forms.CharField(max_length=120, required=False, label="MTN transaction reference")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(is_active=True).order_by("full_name")
        self.fields["customer"].widget.attrs["class"] = "form-select"
        for field_name in ("new_customer_name", "new_customer_phone", "new_customer_email"):
            self.fields[field_name].widget.attrs["class"] = "form-control"
        self.fields["new_customer_address"].widget.attrs["class"] = "form-control"
        self.fields["momo_reference"].widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        has_new_customer = any(cleaned.get(field) for field in ("new_customer_name", "new_customer_phone", "new_customer_email", "new_customer_address"))
        if has_new_customer and (not cleaned.get("new_customer_name") or not cleaned.get("new_customer_phone")):
            self.add_error("new_customer_phone", "Enter both the new customer's name and phone number.")
        if has_new_customer and cleaned.get("customer"):
            self.add_error("customer", "Choose an existing customer or enter new customer details, not both.")
        if cleaned.get("sale_type") == SaleType.CREDIT and not cleaned.get("customer") and not (
            cleaned.get("new_customer_name") and cleaned.get("new_customer_phone")
        ):
            self.add_error("customer", "A customer is required for a credit sale.")
        return cleaned


class SaleLineForm(forms.Form):
    product = ProductChoiceField(queryset=Product.objects.none(), required=False)
    quantity = forms.IntegerField(min_value=1, required=False)
    unit_identifiers = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Serial numbers / IMEIs",
        help_text="For tracked products, enter one available number per line.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True, stock_quantity__gt=0).select_related("brand")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("product") and not cleaned.get("quantity") and not cleaned.get("unit_identifiers"):
            return cleaned
        if not cleaned.get("product"):
            self.add_error("product", "Choose a product for this line.")
        if not cleaned.get("quantity"):
            self.add_error("quantity", "Enter a quantity for this line.")
        cleaned["unit_identifiers"] = [
            value.strip() for value in (cleaned.get("unit_identifiers") or "").splitlines() if value.strip()
        ]
        return cleaned


class BaseSaleLineFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not any(form.cleaned_data.get("product") for form in self.forms):
            raise forms.ValidationError("Add at least one product.")


SaleLineFormSet = formset_factory(SaleLineForm, formset=BaseSaleLineFormSet, extra=12, max_num=12, validate_max=True)


class PaymentForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    method = forms.ChoiceField(choices=PaymentMethod.choices)
    momo_reference = forms.CharField(max_length=120, required=False, label="MTN transaction reference")


class RejectionForm(forms.Form):
    reason = forms.CharField(min_length=5, widget=forms.Textarea(attrs={"rows": 3}))


class MomoConfirmationForm(forms.Form):
    confirmation_reference = forms.CharField(
        max_length=120,
        help_text="Reference from the verified MTN transaction record or statement.",
    )
