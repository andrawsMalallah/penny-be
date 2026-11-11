import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _

username_validator = RegexValidator(
    regex=r"\A[A-Za-z0-9_]{3,30}\Z",
    message=_("Use 3–30 English letters, numbers or underscores."),
)
display_name_validator = RegexValidator(
    regex=r"\A[A-Za-z\u0621-\u063A\u0641-\u064A\u066E-\u06D3\u06FA-\u06FC\u064B-\u065F\u0670 ]*\Z",
    message=_("Use Arabic or English letters and spaces for the display name."),
)


class PasswordCompositionValidator:
    def validate(self, password: str, user=None) -> None:
        if not all(
            re.search(pattern, password) for pattern in (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^\w\s]|_")
        ):
            raise ValidationError(self.get_help_text(), code="password_composition")

    def get_help_text(self) -> str:
        return _("Include an uppercase letter, lowercase letter, number and symbol.")
