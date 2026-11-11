from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import User


class IdentityDatabaseTests(TestCase):
    def test_case_insensitive_uniqueness_is_database_enforced(self) -> None:
        User.objects.create_user(username="Ali", password=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="ali", password=None)

    def test_database_rejects_invalid_username_from_bulk_write(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(username="invalid username")])

    def test_username_cannot_be_renamed_by_model_save(self) -> None:
        user = User.objects.create_user(username="Ali", password=None)
        user.username = "Renamed"
        with self.assertRaises(ValidationError):
            user.save()
        user.refresh_from_db()
        self.assertEqual(user.username, "Ali")

    def test_password_is_hashed_and_email_optional(self) -> None:
        user = User.objects.create_user(username="Ali", password="Longer!Example49")
        self.assertNotEqual(user.password, "Longer!Example49")
        self.assertTrue(user.check_password("Longer!Example49"))
        self.assertEqual(user.email, "")
