import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chatrooms", "0002_roles_keywords_books"),
        ("rules", "0004_keyword_usages"),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffBizRoomScope",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "biz_key",
                    models.CharField(
                        db_index=True, max_length=64, verbose_name="业务规范名"
                    ),
                ),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "chat_room",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="staff_biz_scopes",
                        to="chatrooms.chatroom",
                        verbose_name="群",
                    ),
                ),
            ],
            options={
                "verbose_name": "员工记单业务群绑定",
                "verbose_name_plural": "员工记单业务群绑定",
                "ordering": ["chat_room_id", "biz_key"],
            },
        ),
        migrations.AddConstraint(
            model_name="staffbizroomscope",
            constraint=models.UniqueConstraint(
                fields=("chat_room", "biz_key"), name="uniq_staff_biz_room_scope"
            ),
        ),
    ]
