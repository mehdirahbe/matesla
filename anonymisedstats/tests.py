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
        response = c.post('/anonymisedstats/car_type')
        self.assertEqual(response.status_code, 200, 'car_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/charge_port_type')
        self.assertEqual(response.status_code, 200, 'charge_port_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/exterior_color')
        self.assertEqual(response.status_code, 200, 'exterior_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/has_air_suspension')
        self.assertEqual(response.status_code, 200, 'has_air_suspension anonymisedstats did fail')
        response = c.post('/anonymisedstats/has_ludicrous_mode')
        self.assertEqual(response.status_code, 200, 'has_ludicrous_mode anonymisedstats did fail')
        response = c.post('/anonymisedstats/motorized_charge_port')
        self.assertEqual(response.status_code, 200, 'motorized_charge_port anonymisedstats did fail')
        response = c.post('/anonymisedstats/rear_seat_heaters')
        self.assertEqual(response.status_code, 200, 'rear_seat_heaters anonymisedstats did fail')
        response = c.post('/anonymisedstats/rhd')
        self.assertEqual(response.status_code, 200, 'rhd anonymisedstats did fail')
        response = c.post('/anonymisedstats/roof_color')
        self.assertEqual(response.status_code, 200, 'roof_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/wheel_type')
        self.assertEqual(response.status_code, 200, 'wheel_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/sentry_mode_available')
        self.assertEqual(response.status_code, 200, 'sentry_mode_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/smart_summon_available')
        self.assertEqual(response.status_code, 200, 'smart_summon_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/eu_vehicle')
        self.assertEqual(response.status_code, 200, 'eu_vehicle anonymisedstats did fail')
        #bar chart on car infos by car type
        self.assertEqual(response.status_code, 200, 'firmwareupdates anonymisedstats did fail')
        response = c.post('/anonymisedstats/car_type/model3')
        self.assertEqual(response.status_code, 200, 'car_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/charge_port_type/model3')
        self.assertEqual(response.status_code, 200, 'charge_port_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/exterior_color/model3')
        self.assertEqual(response.status_code, 200, 'exterior_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/has_air_suspension/model3')
        self.assertEqual(response.status_code, 200, 'has_air_suspension anonymisedstats did fail')
        response = c.post('/anonymisedstats/has_ludicrous_mode/model3')
        self.assertEqual(response.status_code, 200, 'has_ludicrous_mode anonymisedstats did fail')
        response = c.post('/anonymisedstats/motorized_charge_port/model3')
        self.assertEqual(response.status_code, 200, 'motorized_charge_port anonymisedstats did fail')
        response = c.post('/anonymisedstats/rear_seat_heaters/model3')
        self.assertEqual(response.status_code, 200, 'rear_seat_heaters anonymisedstats did fail')
        response = c.post('/anonymisedstats/rhd/model3')
        self.assertEqual(response.status_code, 200, 'rhd anonymisedstats did fail')
        response = c.post('/anonymisedstats/roof_color/model3')
        self.assertEqual(response.status_code, 200, 'roof_color anonymisedstats did fail')
        response = c.post('/anonymisedstats/wheel_type/model3')
        self.assertEqual(response.status_code, 200, 'wheel_type anonymisedstats did fail')
        response = c.post('/anonymisedstats/sentry_mode_available/model3')
        self.assertEqual(response.status_code, 200, 'sentry_mode_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/smart_summon_available/model3')
        self.assertEqual(response.status_code, 200, 'smart_summon_available anonymisedstats did fail')
        response = c.post('/anonymisedstats/eu_vehicle/model3')
        self.assertEqual(response.status_code, 200, 'eu_vehicle anonymisedstats did fail')

    def test_BogusUrlFails(self):
        c = Client()
        response = c.post('/anonymisedstats/')
        self.assertEqual(response.status_code, 404, 'anonymisedstats without params did work')
        response = c.post('/anonymisedstats/field/car_type/crap')
        self.assertEqual(response.status_code, 404, 'anonymisedstats with too much params did work')
