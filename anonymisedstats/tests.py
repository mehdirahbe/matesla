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
        response = c.post('/anonymisedstats/firmwareupdates')
        #bar chart on car infos
        self.assertEqual(response.status_code, 200, 'firmwareupdates anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/car_type')
        self.assertEqual(response.status_code, 200, 'car_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/charge_port_type')
        self.assertEqual(response.status_code, 200, 'charge_port_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/exterior_color')
        self.assertEqual(response.status_code, 200, 'exterior_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/has_air_suspension')
        self.assertEqual(response.status_code, 200, 'has_air_suspension anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/has_ludicrous_mode')
        self.assertEqual(response.status_code, 200, 'has_ludicrous_mode anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/motorized_charge_port')
        self.assertEqual(response.status_code, 200, 'motorized_charge_port anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/rear_seat_heaters')
        self.assertEqual(response.status_code, 200, 'rear_seat_heaters anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/rhd')
        self.assertEqual(response.status_code, 200, 'rhd anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/roof_color')
        self.assertEqual(response.status_code, 200, 'roof_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/wheel_type')
        self.assertEqual(response.status_code, 200, 'wheel_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/sentry_mode_available')
        self.assertEqual(response.status_code, 200, 'sentry_mode_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/smart_summon_available')
        self.assertEqual(response.status_code, 200, 'smart_summon_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/eu_vehicle')
        self.assertEqual(response.status_code, 200, 'eu_vehicle anonymisedstats did fail')
        #bar chart on car infos by car type
        self.assertEqual(response.status_code, 200, 'firmwareupdates anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/car_type/model3')
        self.assertEqual(response.status_code, 200, 'car_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/charge_port_type/model3')
        self.assertEqual(response.status_code, 200, 'charge_port_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/exterior_color/model3')
        self.assertEqual(response.status_code, 200, 'exterior_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/has_air_suspension/model3')
        self.assertEqual(response.status_code, 200, 'has_air_suspension anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/has_ludicrous_mode/model3')
        self.assertEqual(response.status_code, 200, 'has_ludicrous_mode anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/motorized_charge_port/model3')
        self.assertEqual(response.status_code, 200, 'motorized_charge_port anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/rear_seat_heaters/model3')
        self.assertEqual(response.status_code, 200, 'rear_seat_heaters anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/rhd/model3')
        self.assertEqual(response.status_code, 200, 'rhd anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/roof_color/model3')
        self.assertEqual(response.status_code, 200, 'roof_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/wheel_type/model3')
        self.assertEqual(response.status_code, 200, 'wheel_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/sentry_mode_available/model3')
        self.assertEqual(response.status_code, 200, 'sentry_mode_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/smart_summon_available/model3')
        self.assertEqual(response.status_code, 200, 'smart_summon_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/StatsOnCarGraph/eu_vehicle/model3')
        self.assertEqual(response.status_code, 200, 'eu_vehicle anonymisedstats did fail')
        response = c.post('/anonymisedstats/GetAllRawCarInfos')
        self.assertEqual(response.status_code, 200, 'GetAllRawCarInfos anonymisedstats did fail')
        response = c.post('/anonymisedstats/FirmwareUpdatesAsCSV')
        self.assertEqual(response.status_code, 200, 'FirmwareUpdatesAsCSV anonymisedstats did fail')

    def test_BogusUrlFails(self):
        c = Client()
        response = c.post('/anonymisedstats/')
        self.assertEqual(response.status_code, 404, 'anonymisedstats without params did work')
        response = c.post('/anonymisedstats/StatsOnCarGraph/field/car_type/crap')
        self.assertEqual(response.status_code, 404, 'anonymisedstats with too much params did work')
