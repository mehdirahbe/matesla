from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0038_nominatim_daily_quota"),
    ]

    operations = [
        migrations.AddField(
            model_name="teslavehicle",
            name="last_polled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
