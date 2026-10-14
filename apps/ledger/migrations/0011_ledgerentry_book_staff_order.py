from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0010_alter_ledgerentry_business_name_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ledgerentry",
            name="book",
            field=models.CharField(
                choices=[
                    ("ledger", "记账表"),
                    ("staff_order", "员工记单"),
                ],
                db_index=True,
                default="ledger",
                max_length=32,
                verbose_name="账本",
            ),
        ),
    ]
