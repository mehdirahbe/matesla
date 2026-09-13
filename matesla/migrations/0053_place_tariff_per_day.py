from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0052_tesla_history_sync_window"),
    ]

    operations = [
        migrations.AddField(
            model_name="placetariffperiod",
            name="eur_per_day",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="placetariffperiod",
            name="mode",
            field=models.CharField(
                choices=[
                    ("flat", "Flat"),
                    ("day_night", "Day / night"),
                    ("dynamic", "Dynamic (day-ahead + cents)"),
                    ("per_day", "Per day"),
                ],
                default="flat",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="chargesessioncost",
            name="rule",
            field=models.CharField(
                choices=[
                    ("home_flat", "Home (flat)"),
                    ("home_day_night", "Home (day / night)"),
                    ("home_dynamic", "Home (dynamic)"),
                    ("work", "Work"),
                    ("place", "Named place"),
                    ("per_day", "Per day"),
                    ("supercharger_invoice", "Tesla Supercharger invoice"),
                    ("supercharger_rate", "Supercharger average rate"),
                    ("other", "Other chargers"),
                    ("unpriced", "Unpriced"),
                ],
                default="unpriced",
                max_length=32,
            ),
        ),
    ]
