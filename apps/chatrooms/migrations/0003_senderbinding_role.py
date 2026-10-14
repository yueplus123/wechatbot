from django.db import migrations, models


def copy_employee_role_to_bindings(apps, schema_editor):
    SenderBinding = apps.get_model("chatrooms", "SenderBinding")
    for b in SenderBinding.objects.select_related("employee").iterator():
        role = getattr(getattr(b, "employee", None), "role", None) or "staff"
        if role not in {"staff", "admin", "finance"}:
            role = "staff"
        SenderBinding.objects.filter(pk=b.pk).update(role=role)


class Migration(migrations.Migration):
    dependencies = [
        ("chatrooms", "0002_roles_keywords_books"),
    ]

    operations = [
        migrations.AddField(
            model_name="senderbinding",
            name="role",
            field=models.CharField(
                choices=[
                    ("staff", "员工"),
                    ("admin", "管理员"),
                    ("finance", "财务/组长"),
                ],
                default="staff",
                max_length=16,
                verbose_name="本群角色",
            ),
        ),
        migrations.RunPython(copy_employee_role_to_bindings, migrations.RunPython.noop),
    ]
