from django.contrib.auth.forms import PasswordChangeForm

from .services import change_own_password


class RequiredPasswordChangeForm(PasswordChangeForm):
    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)
        return change_own_password(user=self.user, password=self.cleaned_data["new_password1"])
