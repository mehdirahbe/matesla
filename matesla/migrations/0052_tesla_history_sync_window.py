from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0051_tesla_charging_invoice"),
    ]

    operations = [
        migrations.AddField(
            model_name="teslacharginghistorysync",
            name="last_from",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="teslacharginghistorysync",
            name="last_to",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
