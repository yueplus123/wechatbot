# Generated manually for BookRoomScope

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("chatrooms", "0002_roles_keywords_books"),
        ("rules", "0002_roles_keywords_books"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookRoomScope",
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
                ("book", models.CharField(db_index=True, max_length=32, verbose_name="账本")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "chat_room",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="book_scopes",
                        to="chatrooms.chatroom",
                        verbose_name="群",
                    ),
                ),
            ],
            options={
                "verbose_name": "账本群作用域",
                "verbose_name_plural": "账本群作用域",
                "ordering": ["book", "chat_room_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="bookroomscope",
            constraint=models.UniqueConstraint(
                fields=("book", "chat_room"), name="uniq_book_room_scope"
            ),
        ),
    ]
