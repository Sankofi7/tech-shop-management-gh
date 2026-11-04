from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import Role, User


class StaffCreateForm(forms.Form):
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, label="Last name")
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=((Role.SECRETARY, "Secretary"), (Role.EMPLOYEE, "Employee")))
    temporary_password = forms.CharField(
        label="Temporary password", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Share this temporary password with the staff member privately. They must change it at first sign-in.",
    )
    confirm_temporary_password = forms.CharField(
        label="Confirm temporary password", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    is_active = forms.BooleanField(required=False, initial=True, label="Account active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_widgets()

    def _style_widgets(self):
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-select" if isinstance(field.widget, forms.Select) else "form-control"

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already in use.")
        return username

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("temporary_password")
        confirmation = cleaned.get("confirm_temporary_password")
        if password and confirmation and password != confirmation:
            self.add_error("confirm_temporary_password", "The passwords do not match.")
        if password and cleaned.get("username"):
            candidate = User(
                username=cleaned["username"], first_name=cleaned.get("first_name", ""),
                last_name=cleaned.get("last_name", ""), email=cleaned.get("email", ""),
            )
            try:
                validate_password(password, user=candidate)
            except ValidationError as exc:
                self.add_error("temporary_password", exc)
        return cleaned


class StaffEditForm(forms.Form):
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, label="Last name")
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=((Role.SECRETARY, "Secretary"), (Role.EMPLOYEE, "Employee")))

    def __init__(self, *args, staff, **kwargs):
        super().__init__(*args, **kwargs)
        self.staff = staff
        if staff.role == Role.OWNER:
            self.fields.pop("role")
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"


class StaffPasswordResetForm(forms.Form):
    temporary_password = forms.CharField(
        label="New temporary password", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="The account will be required to change this password at its next sign-in.",
    )
    confirm_temporary_password = forms.CharField(
        label="Confirm temporary password", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, staff, **kwargs):
        super().__init__(*args, **kwargs)
        self.staff = staff
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("temporary_password")
        confirmation = cleaned.get("confirm_temporary_password")
        if password and confirmation and password != confirmation:
            self.add_error("confirm_temporary_password", "The passwords do not match.")
        if password:
            try:
                validate_password(password, user=self.staff)
            except ValidationError as exc:
                self.add_error("temporary_password", exc)
        return cleaned
