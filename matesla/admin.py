from django.contrib import admin

# Register your models here.
from .models.AllSuperchargers import AllSuperchargers
from .models.SuperchargerUse import SuperchargerUse
from .models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from .models.TeslaToken import TeslaToken
from .models.TeslaFirmwareHistory import TeslaFirmwareHistory
from .models.TeslaCarInfo import TeslaCarInfo

admin.site.register(TeslaToken)
admin.site.register(TeslaFirmwareHistory)
admin.site.register(TeslaCarInfo)
admin.site.register(TeslaCarDataSnapshot)
admin.site.register(AllSuperchargers)
admin.site.register(SuperchargerUse)
