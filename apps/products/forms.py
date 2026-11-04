from decimal import Decimal

from django import forms

from .models import Brand, Category, MovementType, Product, UnitCondition


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = (
            "name", "category", "brand", "model", "description", "sku",
            "purchase_price", "selling_price", "minimum_stock_level",
            "track_individual_units", "is_active",
        )
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(is_active=True)
        self.fields["brand"].queryset = Brand.objects.filter(is_active=True)
        self.fields["purchase_price"].widget.attrs["step"] = "0.01"
        self.fields["selling_price"].widget.attrs["step"] = "0.01"
        self.fields["minimum_stock_level"].help_text = "Creating a product does not add stock. Receive stock separately."
        for name, field in self.fields.items():
            field.widget.attrs["class"] = (
                "form-check-input" if isinstance(field.widget, forms.CheckboxInput)
                else "form-select" if isinstance(field.widget, forms.Select)
                else "form-control"
            )

    def clean_purchase_price(self):
        value = self.cleaned_data["purchase_price"]
        if value < Decimal("0"):
            raise forms.ValidationError("Purchase price cannot be negative.")
        return value


class StockMovementForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True))
    quantity = forms.IntegerField()
    reference = forms.CharField(max_length=120, required=False)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    unit_identifiers = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "One serial number or IMEI per line"}),
        help_text="For individually tracked products, enter one identifier per unit.",
    )
    unit_condition = forms.ChoiceField(choices=UnitCondition.choices, initial=UnitCondition.NEW)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
    def clean_unit_identifiers(self):
        values = [line.strip() for line in self.cleaned_data["unit_identifiers"].splitlines() if line.strip()]
        if len(set(values)) != len(values):
            raise forms.ValidationError("Each identifier must be unique.")
        return values


class StockReceiveForm(StockMovementForm):
    quantity = forms.IntegerField(min_value=1)


class StockAdjustmentForm(StockMovementForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quantity"].help_text = "Use a positive number to add units or a negative number to remove units."

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        if quantity == 0:
            raise forms.ValidationError("Adjustment quantity cannot be zero.")
        return quantity


class MovementFilterForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.order_by("name"), required=False)
    movement_type = forms.ChoiceField(choices=(("", "All movement types"), *MovementType.choices), required=False)
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
