from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0039_teslavehicle_last_polled_at"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="teslacardatasnapshot",
            index=models.Index(
                fields=["hashedVin", "Date"],
                name="matesla_tes_hashedV_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="teslacardatasnapshot",
            index=models.Index(
                fields=["hashedVin", "DateOnlyDay"],
                name="matesla_tes_hashedV_day_idx",
            ),
        ),
    ]
