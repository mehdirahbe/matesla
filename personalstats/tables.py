# from https://django-tables2.readthedocs.io/en/latest/pages/table-data.html
import django_tables2 as tables
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory


class TeslaFirmwareHistoryTable(tables.Table):
    class Meta:
        model = TeslaFirmwareHistory
        template_name = "django_tables2/bootstrap.html"
        fields = ("Date", "Version")
