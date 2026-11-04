from datetime import timedelta

from django import forms
from django.utils import timezone


PERIOD_CHOICES = (
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last7", "Last 7 days"),
    ("last30", "Last 30 days"),
    ("month", "This month"),
    ("custom", "Custom range"),
)


class ReportDateForm(forms.Form):
    period = forms.ChoiceField(choices=PERIOD_CHOICES, initial="last7")
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["period"].widget.attrs["class"] = "form-select"
        self.fields["date_from"].widget.attrs["class"] = "form-control"
        self.fields["date_to"].widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("period") == "custom":
            if not cleaned.get("date_from") or not cleaned.get("date_to"):
                raise forms.ValidationError("Choose both dates for a custom range.")
            if cleaned["date_from"] > cleaned["date_to"]:
                raise forms.ValidationError("The start date must be on or before the end date.")
        return cleaned

    def get_range(self):
        cleaned = self.cleaned_data
        today = timezone.localdate()
        period = cleaned["period"]
        if period == "today":
            return today, today
        if period == "yesterday":
            yesterday = today - timedelta(days=1)
            return yesterday, yesterday
        if period == "last7":
            return today - timedelta(days=6), today
        if period == "last30":
            return today - timedelta(days=29), today
        if period == "month":
            return today.replace(day=1), today
        return cleaned["date_from"], cleaned["date_to"]
