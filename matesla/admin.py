from django.contrib import admin

from .models.TeslaAppSettings import TeslaAppSettings
from .models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from .models.TeslaOAuthPending import TeslaOAuthPending
from .models.TeslaToken import TeslaToken, TeslaVehicle
from .models.TeslaFirmwareHistory import TeslaFirmwareHistory
from .models.TeslaCarInfo import TeslaCarInfo

admin.site.register(TeslaToken)
admin.site.register(TeslaVehicle)
admin.site.register(TeslaAppSettings)
admin.site.register(TeslaOAuthPending)
admin.site.register(TeslaFirmwareHistory)
admin.site.register(TeslaCarInfo)
admin.site.register(TeslaCarDataSnapshot)
