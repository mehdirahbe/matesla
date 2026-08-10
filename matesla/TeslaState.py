# info about car identification+computed values displayed in car status page
class TeslaState:
    vin = None
    name = None
    # battery degradation in %
    batterydegradation = 0.
    location = ""
    isOnline: bool = True
    vehicle_state = None
    OdometerInKm = 0.
    # Raw Tesla API values (miles) — preferred for unit-aware display
    odometer_miles = None
    batteryrange_miles = None
    # Estimated number of cycles of the battery
    NumberCycles: int = None
    # Guessed EPA range in miles
    EPARangeMiles: int = None
