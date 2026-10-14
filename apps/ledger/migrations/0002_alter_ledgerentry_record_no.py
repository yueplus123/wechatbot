from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ledgerentry",
            name="record_no",
            field=models.CharField(max_length=48, unique=True),
        ),
    ]
