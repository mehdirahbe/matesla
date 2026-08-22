import tempfile
from io import BytesIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from PIL import Image

from carimage.models import TeslaImage
from carimage.urls import urlpatterns
from carimage.views import _cache_key, build_compositor_url
from personalstats.test_factories import assert_not_production_database


def _tiny_png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class CarImageTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.TemporaryDirectory(prefix="matesla-carimage-")
        cls._settings = override_settings(MEDIA_ROOT=cls._media.name)
        cls._settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        cls._media.cleanup()
        super().tearDownClass()

    def setUp(self):
        assert_not_production_database()

    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is carimage.urls is empty')

    def test_valid_color_wheel_model_returns_cached_image(self):
        """Valid combo serves image bytes without hitting Tesla compositor."""
        png = _tiny_png_bytes()
        compositor_url = build_compositor_url(
            "PBSB", "Pinwheel18", "model3", size="1920"
        )
        image = TeslaImage(image_url=_cache_key(compositor_url))
        image.image_file.save("image_test.png", ContentFile(png), save=True)

        client = Client()
        with patch("carimage.views.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = AssertionError(
                "valid snapshot must use the cached TeslaImage row"
            )
            response = client.get("/carimage/PBSB/Pinwheel18/model3")
        self.assertEqual(response.status_code, 200)
        body = response.content
        self.assertTrue(
            body.startswith(b"\x89PNG\r\n\x1a\n") or body[:2] == b"\xff\xd8",
            "expected PNG or JPEG magic",
        )
        self.assertGreater(len(body), 8)

    def test_fleet_name_color_and_highland_wheel_ok(self):
        png = _tiny_png_bytes()
        compositor_url = build_compositor_url(
            "DeepBlue", "Glider18", "model3", size="1920"
        )
        image = TeslaImage(image_url=_cache_key(compositor_url))
        image.image_file.save("image_glider.png", ContentFile(png), save=True)
        with patch("carimage.views.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = AssertionError("must not fetch compositor")
            response = Client().get("/carimage/DeepBlue/Glider18/model3")
        self.assertEqual(response.status_code, 200)

    def test_invalid_color_is_4xx(self):
        client = Client()
        with patch("carimage.views.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = AssertionError("invalid color must not fetch")
            response = client.get("/carimage/NOTACOLOR/Pinwheel18/model3")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)
        self.assertNotEqual(response.status_code, 302)

    def test_invalid_wheel_is_4xx(self):
        client = Client()
        with patch("carimage.views.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = AssertionError("invalid wheel must not fetch")
            response = client.get("/carimage/PBSB/somewheel/model3")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)
        self.assertNotEqual(response.status_code, 302)

    def test_invalid_car_type_is_4xx(self):
        client = Client()
        with patch("carimage.views.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = AssertionError("invalid model must not fetch")
            response = client.get("/carimage/PBSB/Pinwheel18/someCarModel")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)
        self.assertNotEqual(response.status_code, 302)

    def test_UrlWorks(self):
        """Garbage wheel/model is an error, not a compositor redirect."""
        client = Client()
        response = client.post("/carimage/PBSB/somewheel/someCarModel")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)

    def test_BadUrlFails(self):
        client = Client()
        response = client.post('/carimage/PBSB/somewheel/')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
        response = client.post('/carimage/PBSB/someCarModel')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
        response = client.post('/carimage/somewheel/someCarModel')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
        response = client.post('/carimage/')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
