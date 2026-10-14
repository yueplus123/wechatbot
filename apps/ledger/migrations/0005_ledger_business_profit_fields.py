from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0004_ledger_status_processed"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="business_name",
            field=models.CharField(
                blank=True, default="", max_length=128, verbose_name="业务名"
            ),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="amount_to_you",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=18,
                verbose_name="给你",
            ),
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="amount_to_me",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=18,
                verbose_name="给我",
            ),
        ),
    ]
