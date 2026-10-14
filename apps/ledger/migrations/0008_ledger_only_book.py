# Generated manually: hard-delete non-ledger books

from django.db import migrations, models


def cleanup_non_ledger(apps, schema_editor):
    LedgerEntry = apps.get_model("ledger", "LedgerEntry")
    Keyword = apps.get_model("rules", "Keyword")
    BookRoomScope = apps.get_model("rules", "BookRoomScope")

    LedgerEntry.objects.exclude(book="ledger").delete()
    Keyword.objects.exclude(book__in=["", "ledger"]).delete()
    BookRoomScope.objects.exclude(book="ledger").delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0007_ledgerentry_book_gpt_recharge"),
        ("rules", "0004_keyword_usages"),
    ]

    operations = [
        migrations.RunPython(cleanup_non_ledger, noop_reverse),
        migrations.AlterField(
            model_name="ledgerentry",
            name="book",
            field=models.CharField(
                choices=[("ledger", "记账表")],
                db_index=True,
                default="ledger",
                max_length=32,
                verbose_name="账本",
            ),
        ),
    ]
