from django.db import migrations
from django.db.models import F


def mark_existing_as_pushed(apps, schema_editor):
    """历史记录视为已处理，避免升级后误批量补推到薪火。"""
    LedgerEntry = apps.get_model("ledger", "LedgerEntry")
    LedgerEntry.objects.filter(xinhuo_pushed_at__isnull=True).update(
        xinhuo_pushed_at=F("created_at")
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0013_ledgerentry_xinhuo_pushed_at"),
    ]

    operations = [
        migrations.RunPython(mark_existing_as_pushed, noop_reverse),
    ]
