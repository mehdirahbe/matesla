from django.test import TestCase
# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/tools/
from django.test import Client

# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/overview/
# Create your tests here.
from anonymisedstats.urls import urlpatterns


class AccountsTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is anonymisedstats.urls is empty')

    def test_UrlWorks(self):
        c = Client()
        allFields = {
            'car_type',
            'charge_port_type',
            'exterior_color',
            'has_air_suspension',
            'has_ludicrous_mode',
            'motorized_charge_port',
            'rear_seat_heaters',
            'rhd',
            'roof_color',
            'wheel_type',
            'sentry_mode_available',
            'smart_summon_available',
            'eu_vehicle',
            'EPARange',
            'isDualMotor',
            'modelYear',
        }
        # bar chart on car infos
        for lang in {"fr", "en"}:
            response = c.post('/'+lang+'/anonymisedstats/firmwareupdates')
            self.assertEqual(response.status_code, 200, 'firmwareupdates anonymisedstats did fail')
            response = c.post('/'+lang+'/anonymisedstats/FirmwareUpdatesAsCSV')
            self.assertEqual(response.status_code, 200, 'FirmwareUpdatesAsCSV anonymisedstats did fail')
            for field in allFields:
                response = c.post('/'+lang+'/anonymisedstats/StatsOnCarGraph/'+field)
                self.assertEqual(response.status_code, 200, field+' anonymisedstats did fail')
                # bar chart on car infos by car type
                response = c.post('/'+lang+'/anonymisedstats/StatsOnCarGraph/'+field+'/model3')
                self.assertEqual(response.status_code, 200, field+' anonymisedstats did fail')

    def test_BogusUrlFails(self):
        c = Client()
        for lang in {"fr", "en"}:
            response = c.post('/'+lang+'/anonymisedstats/')
            self.assertEqual(response.status_code, 404, 'anonymisedstats without params did work')
            response = c.post('/'+lang+'/anonymisedstats/StatsOnCarGraph/field/car_type/crap')
            self.assertEqual(response.status_code, 404, 'anonymisedstats with too much params did work')
