# Generated by Django 2.2.6 on 2020-05-09 12:39

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matesla', '0010_auto_20200509_1222'),
    ]

    operations = [
        migrations.AlterField(
            model_name='teslacardatasnapshot',
            name='climate_keeper_mode',
            field=models.BooleanField(),
        ),
        migrations.AlterField(
            model_name='teslacardatasnapshot',
            name='fast_charger_brand',
            field=models.TextField(null=True),
        ),
        migrations.AlterField(
            model_name='teslacardatasnapshot',
            name='fast_charger_type',
            field=models.TextField(null=True),
        ),
    ]
