from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0005_ledger_business_profit_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="profit_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=18,
                verbose_name="利润",
            ),
        ),
    ]
