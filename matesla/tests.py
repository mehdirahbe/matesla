from django.test import TestCase
# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/tools/
from django.test import Client

# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/overview/
# Create your tests here.
from matesla.BatteryDegradation import GetEPARange
from matesla.urls import urlpatterns
from matesla.views import returnColorFronContext, ValidColorCodes

# all URLs from this app need a logged user
allURLs = {'',
           'matesla/asleep',
           'matesla/getdesiredchargelevel',
           'matesla/getdesiredtemperature',
           'matesla/flash_lights',
           'matesla/honk_horn',
           'matesla/start_climate',
           'matesla/stop_climate',
           'matesla/unlock_car',
           'matesla/lock_car',
           'matesla/AddTeslaAccount',
           'matesla/TeslaServerError',
           'matesla/TeslaServerCmdFail',
           'matesla/NoTeslaVehicules',
           'matesla/ConnectionError',
           'matesla/sentry_start',
           'matesla/sentry_stop',
           'matesla/valet_start',
           'matesla/valet_stop',
           'matesla/chargeport_open',
           'matesla/chargeport_close',
           'matesla/charge_start',
           'matesla/charge_stop'}


class MaTeslaTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is matesla.urls is empty')

    def ColorIsAlwaysValid(self):
        color = returnColorFronContext(None)
        self.assertIsNotNone("returnColorFromContext did return None")
        self.assertIn(color, ValidColorCodes, "returnColorFronContext returned a color not in ValidColorCodes")

    def test_UrlRedirectWithoutLoggedUser(self):
        c = Client()
        for url in allURLs:
            for lang in {"fr", "en"}:
                response = c.post("/" + lang + '/' + url)
                self.assertEqual(response.status_code, 302, lang + ' url ' + url + ' did work without looged user')
            response = c.post('/' + url)
            # test on 302 as it must redirect to a login in right language
            self.assertEqual(response.status_code, 302, 'int url ' + url + ' did work without looged user')

    def test_JsonUrls(self):
        # all URLs from this app need a logged user
        allJsonURLs = {'matesla/statusJson'}
        c = Client()
        for url in allJsonURLs:
            for lang in {"fr", "en"}:
                response = c.post("/" + lang + '/' + url)
                self.assertEqual(response.status_code, 200, lang + ' url ' + url + ' did not work')
            response = c.post('/' + url)
            self.assertEqual(response.status_code, 302, 'int url ' + url + ' did not work')

    def test_VinFunctions(self):
        from matesla.BatteryDegradation import ResolveEPARange
        from matesla.epa_catalog import lookup_epa_miles
        from matesla.VinAnalysis import GetPlantRegionFromVin, WheelInchesFromType

        # Corentin-class: 2019 M3 LR AWD → 310
        vin = "5YJ3E7EB1KF123456"
        EPARange, model, isDual, year = ResolveEPARange(vin, force=True)
        self.assertEqual(model, "3")
        self.assertEqual(year, 2019)
        self.assertEqual(isDual, True)
        self.assertEqual(EPARange, 310)

        # SR+ RWD 2020 (low projected full) → 240
        vin = "5YJ3E7EA4LF123456"
        EPARange, model, isDual, year = ResolveEPARange(
            vin, force=True, battery_range=200.0, battery_level=90.0
        )
        self.assertEqual(model, "3")
        self.assertEqual(year, 2020)
        self.assertEqual(isDual, False)
        self.assertEqual(EPARange, 240)

        # Aram-class: LR RWD same pack as LR AWD — projected full rules out SR
        vin = "5YJ3E7EA5KF349426"
        EPARange, model, isDual, year = ResolveEPARange(
            vin, force=True, battery_range=264.0, battery_level=100.0
        )
        self.assertEqual(isDual, False)
        self.assertEqual(EPARange, 325)

        # Robotbleu-class Highland China LR AWD 18"
        vin = "LRW3E7EK6RC076090"
        epa, meta = lookup_epa_miles(
            vin, wheel_type="Glider18", projected_full_miles=341.0
        )
        self.assertEqual(GetPlantRegionFromVin(vin), "CN")
        self.assertEqual(WheelInchesFromType("Glider18"), 18)
        self.assertEqual(epa, 342)

        vin = "5YJSA7E2XJF123456"
        EPARange, model, isDual, year = ResolveEPARange(vin, force=True)
        self.assertEqual(model, "S")
        self.assertEqual(year, 2018)
        self.assertEqual(isDual, True)
        self.assertEqual(EPARange, 259)
