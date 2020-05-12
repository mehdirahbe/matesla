# Generated by Django 2.2.6 on 2020-05-09 12:22

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matesla', '0009_auto_20200501_1858'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeslaCarDataSnapshot',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vin', models.TextField()),
                ('Date', models.DateField()),
                ('battery_level', models.IntegerField()),
                ('battery_range', models.FloatField()),
                ('charge_limit_soc', models.IntegerField()),
                ('charge_rate', models.FloatField()),
                ('charger_actual_current', models.IntegerField()),
                ('charger_phases', models.IntegerField()),
                ('charger_power', models.IntegerField()),
                ('charger_voltage', models.IntegerField()),
                ('charging_state', models.TextField()),
                ('est_battery_range', models.FloatField()),
                ('fast_charger_brand', models.TextField()),
                ('fast_charger_present', models.BooleanField()),
                ('fast_charger_type', models.TextField()),
                ('max_range_charge_counter', models.IntegerField()),
                ('usable_battery_level', models.IntegerField()),
                ('climate_keeper_mode', models.TextField()),
                ('driver_temp_setting', models.FloatField()),
                ('inside_temp', models.FloatField()),
                ('is_climate_on', models.BooleanField()),
                ('outside_temp', models.FloatField()),
                ('passenger_temp_setting', models.FloatField()),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('power', models.IntegerField()),
                ('speed', models.IntegerField()),
                ('odometer', models.IntegerField()),
            ],
        ),
        migrations.AddIndex(
            model_name='teslacardatasnapshot',
            index=models.Index(fields=['vin'], name='matesla_tes_vin_6ce2ee_idx'),
        ),
        migrations.AddConstraint(
            model_name='teslacardatasnapshot',
            constraint=models.UniqueConstraint(fields=('vin', 'Date'), name='unique version at same date for cardup'),
        ),
    ]