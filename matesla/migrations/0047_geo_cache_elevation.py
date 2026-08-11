# Generated manually for geo cache elevation fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0046_snapshot_active_route_eta"),
    ]

    operations = [
        migrations.AddField(
            model_name="addressfromlatlong",
            name="elevation",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="addressfromlatlong",
            name="elevation_fetched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="addressfromlatlong",
            name="address",
            field=models.TextField(blank=True, default=""),
        ),
    ]
