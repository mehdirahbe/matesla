from django.contrib import admin

# Register your models here.
from .models.TeslaToken import TeslaToken
from .models.TeslaFirmwareHistory import TeslaFirmwareHistory
from .models.TeslaCarInfo import TeslaCarInfo

admin.site.register(TeslaToken)
admin.site.register(TeslaFirmwareHistory)
admin.site.register(TeslaCarInfo)
