from django.db import transaction
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError

from identity.models import User

from .models import CategorySetup, ExpenseCategory

STARTER_CATEGORIES = (
    ("Food & groceries", "الطعام والبقالة"),
    ("Transport", "المواصلات"),
    ("Housing & bills", "السكن والفواتير"),
    ("Health", "الصحة"),
    ("Shopping", "التسوق"),
    ("Entertainment", "الترفيه"),
    ("Education", "التعليم"),
    ("Other", "أخرى"),
)


def categories_for(user: User) -> QuerySet[ExpenseCategory]:
    return ExpenseCategory.objects.filter(user=user)


@transaction.atomic
def initialize_categories(user: User) -> None:
    # All category writes share the finance user lock, including first-use setup.
    User.objects.select_for_update().get(pk=user.pk)
    if CategorySetup.objects.filter(user=user).exists():
        return
    ExpenseCategory.objects.bulk_create(
        [
            ExpenseCategory(user=user, name_en=english, name_ar=arabic)
            for english, arabic in STARTER_CATEGORIES
        ]
    )
    CategorySetup.objects.create(user=user)


@transaction.atomic
def save_category(user: User, values: dict, category_id: int | None = None) -> ExpenseCategory:
    User.objects.select_for_update().get(pk=user.pk)
    initialize_categories(user)
    category = (
        get_object_or_404(categories_for(user), pk=category_id)
        if category_id is not None
        else ExpenseCategory(user=user)
    )
    if category.archived:
        raise ValidationError({"name_en": ["Restore this category before editing it."]})
    for field in ("name_en", "name_ar"):
        name = values[field]
        if (
            name
            and categories_for(user)
            .filter(**{f"{field}__iexact": name})
            .exclude(pk=category_id)
            .exists()
        ):
            raise ValidationError(
                {field: ["This name is already used, including archived categories."]}
            )
        setattr(category, field, name)
    category.save()
    return category


@transaction.atomic
def archive_category(user: User, category_id: int, archived: bool) -> ExpenseCategory:
    User.objects.select_for_update().get(pk=user.pk)
    category = get_object_or_404(categories_for(user), pk=category_id)
    category.archived = archived
    category.save(update_fields=["archived"])
    return category
