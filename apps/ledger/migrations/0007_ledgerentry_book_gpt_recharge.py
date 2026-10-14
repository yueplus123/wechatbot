from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0006_ledgerentry_profit_amount"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ledgerentry",
            name="book",
            field=models.CharField(
                choices=[
                    ("recharge", "充币表"),
                    ("once_card", "一次卡表"),
                    ("vip", "高级会员表"),
                    ("ledger", "记账表"),
                    ("gpt_recharge", "GPT充值表"),
                    ("usdt", "USDT表"),
                    ("underpay", "少充表"),
                ],
                db_index=True,
                default="ledger",
                max_length=32,
                verbose_name="账本",
            ),
        ),
    ]
