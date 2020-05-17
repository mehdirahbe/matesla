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

    def test_VinFunctions(self):
        vin = '5YJ3E7EB1KF123456'
        EPARange, model, isDual, year = GetEPARange(vin)
        self.assertEqual(model, "3", "5YJ3E7EB1KF123456 is from a model 3")
        self.assertEqual(year, 2019, "5YJ3E7EB1KF123456 is from 2019")
        self.assertEqual(isDual, True, "5YJ3E7EB1KF123456 is dual motor")
        self.assertEqual(EPARange, 310, "5YJ3E7EB1KF123456 has a range of 310")
        vin = '5YJ3E7EA4LF123456'
        EPARange, model, isDual, year = GetEPARange(vin)
        self.assertEqual(model, "3", "5YJ3E7EA4LF123456 is from a model 3")
        self.assertEqual(year, 2020, "5YJ3E7EA4LF123456 is from 2020")
        self.assertEqual(isDual, False, "5YJ3E7EA4LF123456 is single motor")
        self.assertEqual(EPARange, 240, "5YJ3E7EA4LF123456 has a range of 240")
        vin = '5YJSA7E2XJF123456'
        EPARange, model, isDual, year = GetEPARange(vin)
        self.assertEqual(model, "S", "5YJSA7E2XJF123456 is from a model S")
        self.assertEqual(year, 2018, "5YJSA7E2XJF123456 is from 2018")
        self.assertEqual(isDual, True, "5YJSA7E2XJF123456 is dual motor")
        self.assertEqual(EPARange, 259, "5YJSA7E2XJF123456 has a range of 259")
