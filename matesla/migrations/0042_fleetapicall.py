# Generated manually for FleetApiCall cost logging

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0041_rename_matesla_tes_hashedv_date_idx_matesla_tes_hashedv_ea7423_idx_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="FleetApiCall",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "kind",
                    models.CharField(
                        choices=[("vehicle_data", "vehicle_data")],
                        default="vehicle_data",
                        max_length=32,
                    ),
                ),
                ("source", models.CharField(blank=True, default="", max_length=32)),
                ("vin", models.TextField(blank=True, db_index=True, null=True)),
                ("hashedVin", models.TextField(blank=True, db_index=True, null=True)),
                ("user_id", models.IntegerField(blank=True, db_index=True, null=True)),
                ("http_status", models.IntegerField(blank=True, null=True)),
                ("billable", models.BooleanField(default=False)),
                ("detail", models.CharField(blank=True, default="", max_length=240)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["hashedVin", "at"],
                        name="matesla_fle_hashedV_idx",
                    ),
                    models.Index(
                        fields=["kind", "billable", "at"],
                        name="matesla_fle_kind_bi_idx",
                    ),
                ],
            },
        ),
    ]
