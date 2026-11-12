import uuid
from decimal import Decimal

from django import forms

from apps.sales.models import PaymentMethod, Sale, SaleItemUnit

from .models import ReturnCondition, ReturnDisposition, ReturnKind, ReturnStatus


class ReturnRequestForm(forms.Form):
    kind = forms.ChoiceField(choices=ReturnKind.choices, label="Request type")
    reason = forms.CharField(
        label="Reason for return or exchange",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Explain why the customer is returning these items."}),
    )

    def __init__(self, *args, sale, remaining_quantities, **kwargs):
        super().__init__(*args, **kwargs)
        self.sale = sale
        self.remaining_quantities = remaining_quantities
        self.fields["kind"].widget.attrs["class"] = "form-select"
        self.fields["reason"].widget.attrs["class"] = "form-control"

        for item in sale.items.select_related("product").all():
            remaining = remaining_quantities.get(item.pk, 0)
            self.fields[f"quantity_{item.pk}"] = forms.IntegerField(
                required=False, min_value=1, max_value=remaining,
                label=f"Quantity to return (up to {remaining})",
                widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": remaining}),
            )
            if item.product.track_individual_units:
                units = SaleItemUnit.objects.filter(
                    sale_item=item, unit__status="SOLD"
                ).exclude(
                    return_links__return_item__return_case__status__in=(
                        ReturnStatus.PENDING_APPROVAL, ReturnStatus.APPROVED,
                    )
                ).select_related("unit").order_by("unit__identifier")
                self.fields[f"units_{item.pk}"] = forms.MultipleChoiceField(
                    required=False,
                    choices=[(unit.unit.identifier, unit.unit.identifier) for unit in units],
                    label="Serial number / IMEI",
                    help_text="Select each individual unit being returned.",
                    widget=forms.CheckboxSelectMultiple,
                )

    def selected_lines(self):
        lines = []
        for item in self.sale.items.select_related("product").all():
            quantity = self.cleaned_data.get(f"quantity_{item.pk}")
            identifiers = self.cleaned_data.get(f"units_{item.pk}", [])
            if quantity:
                if item.product.track_individual_units and len(identifiers) != quantity:
                    self.add_error(f"units_{item.pk}", "Select one serial number or IMEI for every unit being returned.")
                    continue
                lines.append({
                    "sale_item": item.pk,
                    "quantity": quantity,
                    "unit_identifiers": identifiers,
                })
            elif identifiers:
                self.add_error(f"quantity_{item.pk}", "Enter a quantity for the selected serial number or IMEI.")
        if not lines and not self.errors:
            raise forms.ValidationError("Select at least one product and enter its return quantity.")
        return lines


class ReturnApprovalForm(forms.Form):
    """Collect the complete inspection required by the atomic approval service."""

    def __init__(self, *args, return_case, **kwargs):
        super().__init__(*args, **kwargs)
        self.return_case = return_case
        for item in return_case.items.select_related("sale_item__product").all():
            self.fields[f"condition_{item.pk}"] = forms.ChoiceField(
                choices=ReturnCondition.choices,
                label=f"Condition for {item.sale_item.product.name}",
                widget=forms.Select(attrs={"class": "form-select"}),
            )
            self.fields[f"disposition_{item.pk}"] = forms.ChoiceField(
                choices=ReturnDisposition.choices,
                label=f"Disposition for {item.sale_item.product.name}",
                widget=forms.Select(attrs={"class": "form-select"}),
            )

    def item_decisions(self):
        return {
            item.pk: {
                "condition": self.cleaned_data[f"condition_{item.pk}"],
                "disposition": self.cleaned_data[f"disposition_{item.pk}"],
            }
            for item in self.return_case.items.all()
        }


class ReturnRejectionForm(forms.Form):
    reason = forms.CharField(
        min_length=5,
        label="Reason for rejection",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Explain why this request is being rejected."}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].widget.attrs["class"] = "form-control"

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if len(reason) < 5:
            raise forms.ValidationError("Enter a meaningful rejection reason of at least five characters.")
        return reason


class RefundRequestForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), label="Requested amount (GH₵)")
    method = forms.ChoiceField(choices=PaymentMethod.choices, label="Preferred refund method")
    reason = forms.CharField(
        min_length=5,
        label="Reason for refund",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Explain why a refund is requested."}),
    )

    def __init__(self, *args, refundable_amount, **kwargs):
        super().__init__(*args, **kwargs)
        self.refundable_amount = refundable_amount
        self.fields["amount"].widget.attrs.update({"class": "form-control", "step": "0.01", "max": str(refundable_amount)})
        self.fields["method"].widget.attrs["class"] = "form-select"
        self.fields["reason"].widget.attrs["class"] = "form-control"

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if len(reason) < 5:
            raise forms.ValidationError("Enter a meaningful refund reason of at least five characters.")
        return reason


class RefundRejectionForm(ReturnRejectionForm):
    reason = forms.CharField(
        min_length=5,
        label="Reason for refund rejection",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Explain why this refund request is being rejected."}),
    )


class RefundMomoRejectionForm(ReturnRejectionForm):
    reason = forms.CharField(
        min_length=5,
        label="Reason for MTN MoMo refund rejection",
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Explain why this MTN MoMo refund was rejected."}),
    )


class RefundDisbursementForm(forms.Form):
    """Records a disbursement using the payment method already approved."""

    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), label="Amount paid (GH₵)")
    momo_reference = forms.CharField(
        required=False, max_length=120, label="MTN MoMo transaction reference",
        widget=forms.TextInput(attrs={"placeholder": "Enter the MTN transaction reference."}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, refund_request, remaining_amount, **kwargs):
        super().__init__(*args, **kwargs)
        self.refund_request = refund_request
        self.remaining_amount = remaining_amount
        self.fields["amount"].widget.attrs.update({"class": "form-control", "step": "0.01", "max": str(remaining_amount)})
        self.fields["momo_reference"].widget.attrs["class"] = "form-control"
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4()
        if refund_request.method == PaymentMethod.CASH:
            self.fields.pop("momo_reference")
        else:
            self.fields["momo_reference"].required = True

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount > self.remaining_amount:
            raise forms.ValidationError("The amount exceeds the remaining approved refund amount.")
        return amount

    def clean_momo_reference(self):
        reference = self.cleaned_data.get("momo_reference", "").strip()
        if self.refund_request.method == PaymentMethod.MTN_MOMO and not reference:
            raise forms.ValidationError("Enter the MTN refund transaction reference.")
        return reference


class RefundMomoConfirmationForm(forms.Form):
    confirmation_reference = forms.CharField(
        max_length=120, label="MoMo confirmation reference",
        widget=forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "Confirmation reference"}),
    )

    def clean_confirmation_reference(self):
        reference = self.cleaned_data["confirmation_reference"].strip()
        if not reference:
            raise forms.ValidationError("Enter the MoMo confirmation reference.")
        return reference


class ExchangeSettlementForm(forms.Form):
    """Choose the already-approved replacement credit sale for an exchange."""

    replacement_sale = forms.ModelChoiceField(
        queryset=Sale.objects.none(),
        empty_label="Choose the replacement credit sale",
        label="Replacement sale",
    )

    def __init__(self, *args, replacement_sales, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["replacement_sale"].queryset = replacement_sales
        self.fields["replacement_sale"].widget.attrs["class"] = "form-select"
        self.fields["replacement_sale"].label_from_instance = (
            lambda sale: f"{sale.reference} · GH₵{sale.total:.2f} · {sale.created_at:%d %b %Y}"
        )
