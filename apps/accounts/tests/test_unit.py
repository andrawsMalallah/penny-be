from django.contrib.auth.password_validation import CommonPasswordValidator, validate_password
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.accounts.models import User
from apps.accounts.validators import username_validator


class IdentityValidationTests(SimpleTestCase):
    def test_username_boundaries_and_alphabet(self) -> None:
        for username in ["ab_", "A" * 30, "Ali_123"]:
            username_validator(username)
        for username in ["ab", "A" * 31, "علي", "ali space", "ali\n", "ali@"]:
            with self.subTest(username=username), self.assertRaises(ValidationError):
                username_validator(username)

    def test_display_name_trims_and_falls_back(self) -> None:
        user = User(username="Ali", display_name="  علي Smith  ")
        user.clean()
        self.assertEqual(user.get_full_name(), "علي Smith")
        user.display_name = "   "
        user.clean()
        self.assertEqual(user.get_short_name(), "Ali")

    def test_display_name_rejects_unsupported_characters(self) -> None:
        with self.assertRaises(ValidationError):
            User(username="Ali", display_name="<script>").clean()

    def test_password_requirements(self) -> None:
        validate_password("Longer!Example49")
        for password in [
            "Short!3",
            "longer!example49",
            "LONGER!EXAMPLE49",
            "Longer!ExampleXX",
            "LongerExample49",
        ]:
            with self.subTest(password=password), self.assertRaises(ValidationError):
                validate_password(password)

    def test_common_passwords_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CommonPasswordValidator().validate("password")
