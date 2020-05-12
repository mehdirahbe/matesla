# Generated by Django 2.2.6 on 2020-05-09 17:08

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matesla', '0013_auto_20200509_1247'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='teslacardatasnapshot',
            name='unique version at same date for cardup',
        ),
        migrations.RemoveConstraint(
            model_name='teslacarinfo',
            name='unique version of each car',
        ),
        migrations.RemoveConstraint(
            model_name='teslafirmwarehistory',
            name='unique version at same date for car',
        ),
        migrations.AddConstraint(
            model_name='teslacardatasnapshot',
            constraint=models.UniqueConstraint(fields=('vin', 'Date'), name='TeslaCarDataSnapshot: unique version at same date for car'),
        ),
        migrations.AddConstraint(
            model_name='teslacarinfo',
            constraint=models.UniqueConstraint(fields=('vin',), name='TeslaCarInfo: unique version of each car'),
        ),
        migrations.AddConstraint(
            model_name='teslafirmwarehistory',
            constraint=models.UniqueConstraint(fields=('vin', 'Version', 'Date'), name='TeslaFirmwareHistory: unique version at same date for car'),
        ),
    ]