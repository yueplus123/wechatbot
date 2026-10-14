from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="gatewayheartbeat",
            name="discovered_chats",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Worker 从微信同步的群/会话名列表",
            ),
        ),
    ]
