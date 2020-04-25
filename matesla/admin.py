from django.contrib import admin

# Register your models here.
from .models import TeslaToken, TeslaFirmwareHistory

admin.site.register(TeslaToken)
admin.site.register(TeslaFirmwareHistory)
