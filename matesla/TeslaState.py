# info about car identification+computed values displayed in car status page
class TeslaState:
    vin = None
    name = None
    batterydegradation = 0.
    location = ""
    isOnline: bool = True
    vehicle_state = None
    OdometerInKm = 0.
