from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from identity.models import User

from .categories import STARTER_CATEGORIES, initialize_categories
from .models import CategorySetup, ExpenseCategory


class CategoryTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            "categories@example.com", language="en", email_verified=True
        )
        self.other = User.objects.create_user(
            "other-categories@example.com", language="ar", email_verified=True
        )
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.root = "/api/v1/expense-categories/"

    def post(self, suffix: str = "", values: dict | None = None):
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        return self.client.post(
            self.root + suffix, values or {}, format="json", HTTP_X_CSRFTOKEN=token
        )

    def test_reads_do_not_initialize_and_setup_is_idempotent(self) -> None:
        response = self.client.get(self.root)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertFalse(CategorySetup.objects.exists())
        self.assertEqual(self.post("initialize/").status_code, 204)
        self.assertEqual(self.post("initialize/").status_code, 204)
        self.assertEqual(CategorySetup.objects.count(), 1)
        self.assertEqual(
            list(ExpenseCategory.objects.values_list("name_en", "name_ar")),
            list(STARTER_CATEGORIES),
        )
        self.assertEqual(self.client.get(self.root).json()["count"], 8)

    def test_renamed_archived_starters_never_reappear(self) -> None:
        initialize_categories(self.user)
        category = ExpenseCategory.objects.first()
        self.assertEqual(
            self.post(f"{category.pk}/", {"name_en": "Meals", "name_ar": "وجبات"}).status_code, 200
        )
        self.assertEqual(self.post(f"{category.pk}/archive/", {"archived": True}).status_code, 200)
        self.post("initialize/")
        self.assertEqual(ExpenseCategory.objects.count(), 8)
        category.refresh_from_db()
        self.assertEqual(category.name_en, "Meals")
        self.assertTrue(category.archived)
        self.assertEqual(self.client.get(self.root).json()["count"], 7)
        self.assertEqual(
            self.client.get(self.root + "?archived=true").json()["results"][0]["id"], category.pk
        )
        self.assertEqual(self.post(f"{category.pk}/archive/", {"archived": False}).status_code, 200)

    def test_custom_category_allows_either_name_and_trims_names(self) -> None:
        for values in [
            {"name_en": "  Pets  "},
            {"name_ar": "  هدايا  "},
            {"name_en": "Travel", "name_ar": "السفر"},
        ]:
            response = self.post(values=values)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(set(response.json()), {"id", "name_en", "name_ar", "archived"})
        self.assertTrue(ExpenseCategory.objects.filter(name_en="Pets", name_ar="").exists())
        self.assertTrue(ExpenseCategory.objects.filter(name_en="", name_ar="هدايا").exists())

    def test_blank_long_and_unsupported_fields_are_rejected(self) -> None:
        for values in [
            {},
            {"name_en": "  ", "name_ar": " "},
            {"name_en": "x" * 101},
            {"name_ar": None},
            {"name_en": "X", "user": self.other.pk},
            {"name_en": "X", "archived": True},
        ]:
            with self.subTest(values=values):
                self.assertEqual(self.post(values=values).status_code, 400)
        self.assertFalse(ExpenseCategory.objects.exists())
        self.assertEqual(self.post("initialize/", {"user": self.other.pk}).status_code, 400)

    def test_names_are_unique_per_language_including_archived(self) -> None:
        category = self.post(values={"name_en": "Pets", "name_ar": "حيوانات"}).json()
        self.assertEqual(
            self.post(f"{category['id']}/", {"name_en": "PETS", "name_ar": "حيوانات"}).status_code,
            200,
        )
        self.post(f"{category['id']}/archive/", {"archived": True})
        for values, field in [
            ({"name_en": "pets"}, "name_en"),
            ({"name_ar": "حيوانات"}, "name_ar"),
        ]:
            response = self.post(values=values)
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.json())
        self.assertEqual(self.post(f"{category['id']}/", {"name_en": "Changed"}).status_code, 400)
        self.client.force_login(self.other)
        self.assertEqual(
            self.post(values={"name_en": "Pets", "name_ar": "حيوانات"}).status_code, 201
        )

    def test_private_detail_update_archive_and_no_delete(self) -> None:
        category = self.post(values={"name_en": "Private"}).json()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.root).json()["count"], 0)
        self.assertEqual(self.client.get(self.root + f"{category['id']}/").status_code, 404)
        self.assertEqual(self.post(f"{category['id']}/", {"name_en": "Stolen"}).status_code, 404)
        self.assertEqual(
            self.post(f"{category['id']}/archive/", {"archived": True}).status_code, 404
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.delete(self.root + f"{category['id']}/").status_code, 403)
        token = self.client.get("/api/v1/auth/csrf/").json()["csrf_token"]
        self.assertEqual(
            self.client.delete(
                self.root + f"{category['id']}/", HTTP_X_CSRFTOKEN=token
            ).status_code,
            405,
        )
        self.assertEqual(self.client.get(self.root + f"{category['id']}/").json(), category)

    def test_guest_unverified_and_csrf_boundaries(self) -> None:
        for unverified in [False, True]:
            self.client.logout()
            if unverified:
                self.other.email_verified = False
                self.other.save(update_fields=["email_verified"])
                self.client.force_login(self.other)
            self.assertEqual(self.client.get(self.root).status_code, 403)
            self.assertEqual(self.post("initialize/").status_code, 403)
            self.assertEqual(self.post(values={"name_en": "No"}).status_code, 403)
        self.client.force_login(self.user)
        for suffix, values in [
            ("initialize/", {}),
            ("", {"name_en": "No"}),
            ("1/", {"name_en": "No"}),
            ("1/archive/", {"archived": True}),
        ]:
            self.assertEqual(
                self.client.post(self.root + suffix, values, format="json").status_code, 403
            )
        self.assertFalse(CategorySetup.objects.exists())

    def test_pagination_archive_validation_and_no_query_per_row(self) -> None:
        initialize_categories(self.user)
        ExpenseCategory.objects.bulk_create(
            [ExpenseCategory(user=self.user, name_en=f"Category {index}") for index in range(33)]
        )
        with self.assertNumQueries(1):
            # Authentication/session queries are outside this direct serialization check.
            from .serializers import CategorySerializer

            self.assertEqual(
                len(
                    CategorySerializer(
                        ExpenseCategory.objects.filter(user=self.user)[:30], many=True
                    ).data
                ),
                30,
            )
        first = self.client.get(self.root).json()
        second = self.client.get(self.root + "?page=2").json()
        self.assertEqual(first["count"], 41)
        self.assertEqual(len(first["results"]), 30)
        self.assertEqual(len(second["results"]), 11)
        self.assertTrue(first["next"])
        self.assertTrue(second["previous"])
        self.assertEqual(self.client.get(self.root + "?archived=bad").status_code, 400)
        self.assertEqual(
            self.post("1/archive/", {"archived": True, "name_en": "No"}).status_code, 400
        )

    def test_setup_rolls_back_all_records_on_failure(self) -> None:
        with patch(
            "finance.categories.CategorySetup.objects.create", side_effect=RuntimeError("fixture")
        ):
            with self.assertRaises(RuntimeError):
                initialize_categories(self.user)
        self.assertFalse(ExpenseCategory.objects.exists())
        self.assertFalse(CategorySetup.objects.exists())

    def test_database_constraints_protect_direct_writes(self) -> None:
        for values in [{"name_en": "", "name_ar": ""}, {"name_en": "PETS"}]:
            ExpenseCategory.objects.get_or_create(user=self.user, name_en="Pets")
            with self.assertRaises(IntegrityError), transaction.atomic():
                ExpenseCategory.objects.create(user=self.user, **values)
