# Index for geo elevation propagate / lat-lon range lookups

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0047_geo_cache_elevation"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="teslacardatasnapshot",
            index=models.Index(
                fields=["latitude", "longitude"],
                name="matesla_snap_lat_lon_idx",
            ),
        ),
    ]
