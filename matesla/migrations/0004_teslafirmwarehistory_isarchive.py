# Generated by Django 2.2.6 on 2020-04-26 19:50

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matesla', '0003_teslafirmwarehistory'),
    ]

    operations = [
        migrations.AddField(
            model_name='teslafirmwarehistory',
            name='IsArchive',
            field=models.BooleanField(default=False),
        ),
    ]
