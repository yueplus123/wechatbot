from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0012_ledgerentry_book_usdt"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="xinhuo_pushed_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="薪火已推送于",
            ),
        ),
    ]
