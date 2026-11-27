from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0010_recurringschedule_recurringoutcome_recurringwindow_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="recurringschedule",
            name="deleted_at",
            field=models.DateTimeField(default=None, null=True),
        ),
    ]
