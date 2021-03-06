# Generated by Django 3.0.6 on 2020-05-21 13:25

import datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matesla', '0018_teslacardatasnapshot_dateonlyday'),
    ]

    operations = [
        migrations.CreateModel(
            name='AddressFromLatLong',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('address', models.TextField()),
                ('date', models.DateField(default=datetime.date(2020, 5, 21))),
            ],
        ),
        migrations.AddIndex(
            model_name='addressfromlatlong',
            index=models.Index(fields=['latitude', 'longitude'], name='matesla_add_latitud_2631c2_idx'),
        ),
        migrations.AddConstraint(
            model_name='addressfromlatlong',
            constraint=models.UniqueConstraint(fields=('latitude', 'longitude'), name='AddressFromLatLong: unique address for same latitude and longiture'),
        ),
    ]
