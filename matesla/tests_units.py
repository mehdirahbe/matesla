"""Distance unit preference helpers."""

from django.test import RequestFactory, SimpleTestCase, TestCase

from matesla.units import (
    COOKIE_NAME,
    DEFAULT_DISTANCE_UNIT,
    UNIT_KM,
    UNIT_MI,
    format_distance,
    format_epa_range,
    format_energy_intensity,
    get_distance_unit,
    kwh_per_100km_to_display,
    miles_to_display,
    mph_to_display,
    normalize_unit,
    redirect_url_for_unit,
    with_query_param,
)


class UnitsHelpersTests(SimpleTestCase):
    def test_normalize_unit(self):
        self.assertEqual(normalize_unit("km"), UNIT_KM)
        self.assertEqual(normalize_unit("MI"), UNIT_MI)
        self.assertEqual(normalize_unit("miles"), UNIT_MI)
        self.assertEqual(normalize_unit("nope"), DEFAULT_DISTANCE_UNIT)

    def test_miles_to_display(self):
        self.assertAlmostEqual(miles_to_display(100, UNIT_KM), 160.9344, places=4)
        self.assertEqual(miles_to_display(100, UNIT_MI), 100.0)

    def test_mph_to_display(self):
        self.assertAlmostEqual(mph_to_display(60, UNIT_KM), 96.56064, places=4)
        self.assertEqual(mph_to_display(60, UNIT_MI), 60.0)

    def test_energy_intensity(self):
        # 15 kWh/100 km → Wh/mi
        wh_mi = kwh_per_100km_to_display(15.0, UNIT_MI)
        self.assertAlmostEqual(wh_mi, 15.0 * 16.09344, places=3)
        self.assertEqual(kwh_per_100km_to_display(15.0, UNIT_KM), 15.0)

    def test_format_epa_range(self):
        self.assertEqual(format_epa_range(341, UNIT_MI, decimals=0), "341 mi")
        text = format_epa_range(341, UNIT_KM, decimals=0)
        self.assertIn("km", text)
        self.assertIn("(341 mi)", text)

    def test_format_distance(self):
        self.assertEqual(format_distance(10, UNIT_MI, decimals=0), "10 mi")
        self.assertIn("km", format_distance(10, UNIT_KM, decimals=0))

    def test_format_energy_intensity(self):
        self.assertIn("kWh/100 km", format_energy_intensity(16.5, UNIT_KM))
        self.assertIn("Wh/mi", format_energy_intensity(16.5, UNIT_MI))

    def test_cookie_preference(self):
        factory = RequestFactory()
        request = factory.get("/")
        self.assertEqual(get_distance_unit(request), UNIT_KM)
        request.COOKIES[COOKIE_NAME] = "mi"
        self.assertEqual(get_distance_unit(request), UNIT_MI)

    def test_cache_bust_query(self):
        self.assertEqual(with_query_param("/en/", "_du", "mi"), "/en/?_du=mi")
        self.assertEqual(
            with_query_param("/en/?period=1&_du=km", "_du", "mi"),
            "/en/?period=1&_du=mi",
        )
        self.assertEqual(redirect_url_for_unit("/fr/stats", "mi"), "/fr/stats?_du=mi")


class SetDistanceUnitViewTests(TestCase):
    def test_set_unit_cookie_and_redirect(self):
        from django.test import RequestFactory
        from django.utils import translation

        from matesla.views import view_set_distance_unit

        factory = RequestFactory()
        request = factory.get(
            "/matesla/set-distance-unit",
            {"unit": "mi", "next": "/en/"},
        )
        with translation.override("en"):
            response = view_set_distance_unit(request)
        self.assertEqual(response.status_code, 302)
        # Cache-busted so the browser does not reuse HTML rendered in km
        self.assertEqual(response.url, "/en/?_du=mi")
        self.assertEqual(response.cookies[COOKIE_NAME].value, "mi")
        self.assertIn("no-store", response.get("Cache-Control", ""))

    def test_redirect_preserves_existing_query(self):
        from django.test import RequestFactory

        from matesla.views import view_set_distance_unit

        factory = RequestFactory()
        request = factory.get(
            "/matesla/set-distance-unit",
            {"unit": "km", "next": "/en/personalstats/x?period=52"},
        )
        response = view_set_distance_unit(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/en/personalstats/x?period=52&_du=km")

    def test_rejects_open_redirect(self):
        from django.test import RequestFactory

        from matesla.views import view_set_distance_unit

        factory = RequestFactory()
        request = factory.get(
            "/matesla/set-distance-unit",
            {"unit": "km", "next": "https://evil.example/phish"},
        )
        response = view_set_distance_unit(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/?_du=km")
        self.assertEqual(response.cookies[COOKIE_NAME].value, "km")
