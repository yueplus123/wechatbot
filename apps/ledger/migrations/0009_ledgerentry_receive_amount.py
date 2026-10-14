from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0008_ledger_only_book"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="receive_amount",
            field=models.DecimalField(
                "金额",
                max_digits=18,
                decimal_places=2,
                default=0,
            ),
        ),
        migrations.AlterField(
            model_name="ledgerentry",
            name="status",
            field=models.CharField(
                choices=[
                    ("ACTIVE", "未清账"),
                    ("REVERSED", "已撤销"),
                    ("PROCESSED", "已清账"),
                ],
                default="ACTIVE",
                max_length=16,
            ),
        ),
    ]
