from django.test import TestCase
# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/tools/
from django.test import Client
from django.urls import path

# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/overview/
# Create your tests here.
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
                self.assertEqual(response.status_code, 302, lang + ' url '+url+' did work without looged user')
            response = c.post('/' + url)
            # test on 302 as it must redirect to a login in right language
            self.assertEqual(response.status_code, 302, 'int url '+url+' did work without looged user')
