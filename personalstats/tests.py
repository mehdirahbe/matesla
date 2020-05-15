from django.test import TestCase
# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/tools/
from django.test import Client

# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/overview/
# Create your tests here.
from personalstats.urls import urlpatterns


class PersonalStatsTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is personalstats.urls is empty')

    def test_UrlWorks(self):
        c = Client()
        allFields = {
                "outside_temp",
                "driver_temp_setting",
                "inside_temp",
                "passenger_temp_setting",
                "odometer",
                "speed",
                "latitude",
                "longitude",
                "power",
                "battery_level",
                "battery_range",
                "charge_limit_soc",
                "charge_rate",
                "charger_actual_current",
                "charger_phases",
                "charger_power",
                "charger_voltage",
                "est_battery_range",
                "usable_battery_level",
        }

        # bar chart on car infos
        for lang in {"fr", "en"}:
            response = c.post('/' + lang + '/personalstats/Stats/fakesha')
            self.assertEqual(response.status_code, 200, 'HTML personalstats page did fail')
            for field in allFields:
                response = c.post('/'+lang+'/personalstats/StatsOnCarGraph/fakesha/'+field+'/5')
                self.assertEqual(response.status_code, 200, field+' personalstats did fail')

    def test_BogusUrlFails(self):
        c = Client()
        for lang in {"fr", "en"}:
            response = c.post('/'+lang+'/personalstats/StatsOnCarGraph/fakesha/dontexist/5')
            self.assertEqual(response.status_code, 404, 'personalstats with bogus field did work')
            response = c.post('/' + lang + '/personalstats/Stats/--')
            self.assertEqual(response.status_code, 404, 'HTML personalstats page did work with SQL injection SHA')
