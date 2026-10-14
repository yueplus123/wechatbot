# Generated manually: restore USDT as separate book (same mode as ledger)

from django.db import migrations, models


def forwards(apps, schema_editor):
    LedgerEntry = apps.get_model("ledger", "LedgerEntry")
    Keyword = apps.get_model("rules", "Keyword")
    BookRoomScope = apps.get_model("rules", "BookRoomScope")

    # 历史：ledger + business_name=USDT → book=usdt
    LedgerEntry.objects.filter(book="ledger", business_name__iexact="USDT").update(
        book="usdt"
    )
    Keyword.objects.filter(keyword__iexact="usdt", book="ledger").update(book="usdt")

    # 已开记账的群：若有 USDT 记录，自动勾选 usdt，避免升级后指令失效
    room_ids = (
        LedgerEntry.objects.filter(book="usdt")
        .exclude(chat_room_id=None)
        .values_list("chat_room_id", flat=True)
        .distinct()
    )
    for rid in room_ids:
        obj, created = BookRoomScope.objects.get_or_create(
            book="usdt",
            chat_room_id=rid,
            defaults={"enabled": True},
        )
        if not created and not obj.enabled:
            obj.enabled = True
            obj.save(update_fields=["enabled", "updated_at"])


def backwards(apps, schema_editor):
    LedgerEntry = apps.get_model("ledger", "LedgerEntry")
    Keyword = apps.get_model("rules", "Keyword")
    LedgerEntry.objects.filter(book="usdt").update(book="ledger", business_name="USDT")
    Keyword.objects.filter(keyword__iexact="usdt", book="usdt").update(book="ledger")


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0011_ledgerentry_book_staff_order"),
        ("rules", "0005_staff_biz_room_scope"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ledgerentry",
            name="book",
            field=models.CharField(
                choices=[
                    ("ledger", "记账表"),
                    ("usdt", "USDT"),
                    ("staff_order", "员工记单"),
                ],
                db_index=True,
                default="ledger",
                max_length=32,
                verbose_name="账本",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
