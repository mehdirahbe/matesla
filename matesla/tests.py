from django.test import Client, SimpleTestCase, TestCase

from matesla.BatteryDegradation import (
    ComputeBatteryDegradationFromEPARange,
    ComputeNumCycles,
    ResolveEPARange,
    compute_usable_capacity_and_stored_kwh,
    estimate_new_pack_kwh,
)
from matesla.epa_catalog import lookup_epa_miles, lookup_pack_kwh, project_full_charge_miles
from matesla.models.VinHash import HashTheVin, IsValidHash
from matesla.soc_refine import is_whole_percent, refine_soc_percent
from matesla.urls import urlpatterns
from matesla.views import returnColorFronContext, ValidColorCodes
from matesla.VinAnalysis import (
    GetModelFromVin,
    GetPlantRegionFromVin,
    GetVinDecoderUrl,
    GetYearFromVin,
    IsDualMotor,
    IsPerformanceMotor,
    WheelInchesFromType,
)
from mysite.test_helpers import configured_language_codes


# URLs that require login (or redirect when anonymous). Status/read-only only —
# vehicle command endpoints were removed (Fleet Vehicle Command Protocol).
# Endpoints that hit live Tesla OAuth / vehicle_data are intentionally omitted.
# Root "" is home → day map (DB only); status lives at matesla/status.
allURLs = {
    "",
    "matesla/status",
    "matesla/asleep",
    "matesla/AddTeslaAccount",
    "matesla/TeslaServerError",
    "matesla/NoTeslaVehicules",
    "matesla/ConnectionError",
}


class MaTeslaTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is matesla.urls is empty')

    def test_color_is_always_valid(self):
        color = returnColorFronContext({})
        self.assertIsNotNone(color)
        self.assertIn(color, ValidColorCodes)

    def test_home_redirects_to_day_map_without_fleet(self):
        """Landing is day map for primary vehicle — no vehicle_data cost."""
        from django.contrib.auth import get_user_model

        from matesla.models.TeslaToken import TeslaVehicle
        from matesla.models.VinHash import HashTheVin

        User = get_user_model()
        user = User.objects.create_user("home_user", password="x")
        vin = "5YJ3E7EB1KF000001"
        TeslaVehicle.objects.create(
            user=user,
            api_id="12345",
            vin=vin,
            display_name="Testcar",
            is_primary=True,
        )
        client = Client()
        self.assertTrue(client.login(username="home_user", password="x"))
        response = client.get("/en/", follow=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/personalstats/DayMap/{HashTheVin(vin)}", response["Location"])

    def test_UrlRedirectWithoutLoggedUser(self):
        client = Client()
        for url in allURLs:
            for lang in configured_language_codes():
                response = client.post("/" + lang + "/" + url)
                self.assertEqual(
                    response.status_code,
                    302,
                    lang + " url " + url + " did work without looged user",
                )
            response = client.post("/" + url)
            # test on 302 as it must redirect to a login in right language
            self.assertEqual(
                response.status_code, 302, "int url " + url + " did work without looged user"
            )

    def test_JsonUrls(self):
        # JSON status without login: i18n path returns a JSON error payload (200)
        allJsonURLs = {"matesla/statusJson"}
        client = Client()
        for url in allJsonURLs:
            for lang in configured_language_codes():
                response = client.post("/" + lang + "/" + url)
                self.assertEqual(
                    response.status_code,
                    200,
                    lang + " url " + url + " did not work",
                )
            response = client.post("/" + url)
            self.assertEqual(response.status_code, 302, "int url " + url + " did not work")


class VinAnalysisUnitTests(SimpleTestCase):
    """Pure VIN decode helpers (no DB / no Tesla network)."""

    def test_year_model_motor_from_known_vins(self):
        # Corentin-class 2019 M3 LR AWD
        vin = "5YJ3E7EB1KF123456"
        self.assertEqual(GetYearFromVin(vin), 2019)
        self.assertEqual(GetModelFromVin(vin), "3")
        self.assertIs(IsDualMotor(vin), True)
        self.assertFalse(IsPerformanceMotor(vin))

        # RWD single-motor letter "A"
        vin_rwd = "5YJ3E7EA4LF123456"
        self.assertEqual(GetYearFromVin(vin_rwd), 2020)
        self.assertIs(IsDualMotor(vin_rwd), False)

        # Model S 2018
        vin_s = "5YJSA7E2XJF123456"
        self.assertEqual(GetYearFromVin(vin_s), 2018)
        self.assertEqual(GetModelFromVin(vin_s), "S")

    def test_plant_region_and_wheels(self):
        self.assertEqual(GetPlantRegionFromVin("LRW3E7EK6RC076090"), "CN")
        self.assertEqual(GetPlantRegionFromVin("5YJ3E7EB1KF123456"), "US")
        self.assertEqual(WheelInchesFromType("Glider18"), 18)
        self.assertEqual(WheelInchesFromType("Induction19"), 19)
        self.assertIsNone(WheelInchesFromType(""))
        self.assertIsNone(GetYearFromVin("SHORT"))

    def test_vin_decoder_url_routes_by_wmi(self):
        us_url = GetVinDecoderUrl("5YJ3E7EB1KF123456")
        self.assertIn("nhtsa", us_url.lower())
        self.assertIn("5YJ3E7EB1KF123456", us_url)

        cn_url = GetVinDecoderUrl("LRW3E7EK6RC076090")
        self.assertIn("teslatap", cn_url.lower())

        self.assertIn("teslatap", GetVinDecoderUrl(None).lower())


class VinHashUnitTests(SimpleTestCase):
    def test_is_valid_hash_rejects_injection(self):
        self.assertTrue(IsValidHash("a" * 56))
        self.assertTrue(IsValidHash("abc.def0123"))
        self.assertFalse(IsValidHash(None))
        self.assertFalse(IsValidHash("--"))
        self.assertFalse(IsValidHash("ABC"))  # uppercase not allowed in path token
        self.assertFalse(IsValidHash("drop table;"))
        self.assertFalse(IsValidHash("../etc"))

    def test_hash_the_vin_stable_and_salted(self):
        vin = "5YJ3E7EB1KF123456"
        first = HashTheVin(vin)
        second = HashTheVin(vin)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 56)  # sha224 hex
        self.assertTrue(IsValidHash(first))
        self.assertNotEqual(HashTheVin(vin), HashTheVin(vin[:-1] + "7"))
        self.assertIsNone(HashTheVin(None))


class EpaCatalogUnitTests(SimpleTestCase):
    """Catalog + pure math (no DB)."""

    def test_lookup_epa_miles_robotbleu_highland(self):
        vin = "LRW3E7EK6RC076090"
        epa, _meta = lookup_epa_miles(
            vin, wheel_type="Glider18", projected_full_miles=341.0
        )
        self.assertEqual(epa, 342)

    def test_project_full_charge_and_degradation_math(self):
        self.assertAlmostEqual(project_full_charge_miles(155, 50), 310.0)
        self.assertIsNone(project_full_charge_miles(None, 50))

        # At EPA full range and 100% SoC → ~0% degradation
        self.assertAlmostEqual(
            ComputeBatteryDegradationFromEPARange(310, 100, 310), 0.0, places=5
        )
        # Half EPA range at 100% → 50% degradation
        self.assertAlmostEqual(
            ComputeBatteryDegradationFromEPARange(155, 100, 310), 50.0, places=5
        )
        # Same physical pack at 50% SoC still ~0% if range halves with SoC
        self.assertAlmostEqual(
            ComputeBatteryDegradationFromEPARange(155, 50, 310), 0.0, places=5
        )
        self.assertIsNone(ComputeBatteryDegradationFromEPARange(155, 0, 310))

        cycles = ComputeNumCycles(310, 31000)
        self.assertIsNotNone(cycles)
        # odo/EPA * 1.2 → 100 * 1.2
        self.assertAlmostEqual(cycles, 120.0, places=5)

    def test_refine_soc_from_range(self):
        self.assertTrue(is_whole_percent(64))
        self.assertTrue(is_whole_percent(64.0))
        self.assertFalse(is_whole_percent(64.3))
        # 186 mi remaining of 310 pack → 60%
        refined = refine_soc_percent(60, 186, 310)
        self.assertIsNotNone(refined)
        self.assertAlmostEqual(refined, 60.0, places=1)


class BatteryCapacityKwhTests(SimpleTestCase):
    """Usable capacity + stored energy from the shared pack estimate."""

    def test_pack_catalog_2019_lr_is_75(self):
        # Both 2019 LR (AWD EPA 310 / RWD EPA 325) share ~75 kWh when new
        corentin = "5YJ3E7EB1KF123456"
        aram = "5YJ3E7EA5KF349426"
        self.assertEqual(lookup_pack_kwh(corentin, epa_range_miles=310), 75.0)
        self.assertEqual(lookup_pack_kwh(aram, epa_range_miles=325), 75.0)

    def test_estimate_new_pack_kwh_epa_fallback_only(self):
        # Fallback when no VIN catalog (not used for known household cars)
        self.assertAlmostEqual(estimate_new_pack_kwh(310), 68.2, places=1)
        self.assertEqual(estimate_new_pack_kwh(None), 75.0)

    def test_corentin_remaining_capacity_after_degradation(self):
        # Pack when new 75 kWh; 22.2% deg → ~58.4 kWh left; 20% SoC → ~11.7 kWh
        capacity, stored = compute_usable_capacity_and_stored_kwh(
            pack_kwh_when_new=75.0,
            battery_degradation_percent=22.2,
            usable_battery_level=20.0,
        )
        self.assertAlmostEqual(capacity, 75.0 * (1.0 - 0.222), places=1)
        self.assertAlmostEqual(capacity, 58.35, places=1)
        self.assertAlmostEqual(stored, 75.0 * (1.0 - 0.222) * 0.20, places=1)

    def test_missing_soc_still_returns_capacity(self):
        capacity, stored = compute_usable_capacity_and_stored_kwh(
            pack_kwh_when_new=75.0,
            battery_degradation_percent=22.2,
        )
        self.assertAlmostEqual(capacity, 75.0 * (1.0 - 0.222), places=2)
        self.assertIsNone(stored)


class ResolveEpaRangeTests(TestCase):
    """
    ResolveEPARange may read TeslaCarInfo cache — needs a test DB.

    Still no live Tesla API calls (force=True / catalog path).
    """

    def test_resolve_epa_known_household_cars(self):
        # Corentin-class: 2019 M3 LR AWD → 310
        vin = "5YJ3E7EB1KF123456"
        epa_range, model, is_dual, year = ResolveEPARange(vin, force=True)
        self.assertEqual(model, "3")
        self.assertEqual(year, 2019)
        self.assertEqual(is_dual, True)
        self.assertEqual(epa_range, 310)

        # SR+ RWD 2020 (low projected full) → 240
        vin = "5YJ3E7EA4LF123456"
        epa_range, model, is_dual, year = ResolveEPARange(
            vin, force=True, battery_range=200.0, battery_level=90.0
        )
        self.assertEqual(model, "3")
        self.assertEqual(year, 2020)
        self.assertEqual(is_dual, False)
        self.assertEqual(epa_range, 240)

        # Aram-class: LR RWD same pack as LR AWD — projected full rules out SR
        vin = "5YJ3E7EA5KF349426"
        epa_range, model, is_dual, year = ResolveEPARange(
            vin, force=True, battery_range=264.0, battery_level=100.0
        )
        self.assertEqual(is_dual, False)
        self.assertEqual(epa_range, 325)

        vin = "5YJSA7E2XJF123456"
        epa_range, model, is_dual, year = ResolveEPARange(vin, force=True)
        self.assertEqual(model, "S")
        self.assertEqual(year, 2018)
        self.assertEqual(is_dual, True)
        self.assertEqual(epa_range, 259)
