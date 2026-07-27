import io
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

from django.core.exceptions import ObjectDoesNotExist
from django.core.files import File
from django.http import HttpResponse, HttpResponseRedirect
from PIL import Image

from carimage.models import TeslaImage

# Bump when crop logic changes so cached full-frame images are regenerated.
_IMAGE_CACHE_TAG = "crop-v1"

'''
Tesla configurator / compositor images.

Legacy Model 3 playground (pre-Highland):
  https://observablehq.com/@slickplaid/model-3-configurator

Modern design studio (Highland M3, Juniper MY, refresh MS/MX, Cybertruck):
  https://static-assets.tesla.com/configurator/compositor?context=design_studio_2&...

Examples (from tesla.com design studio):
  M3 Highland:  options=$MT370,$PPSW,$W38A,$IPB3&view=STUD_FRONT34&model=m3
  MY Juniper:   options=$MTY86,$PPSW,$WY19P,$IPB12&view=FRONT34&model=my
  MS refresh:   options=$MTS14,$PPMR,$WS10,$ICC00&view=FRONT34&model=ms
  MX refresh:   options=$MTX13,$PMNG,$WX00,$IWW00&view=FRONT34&model=mx
  Cybertruck:   options=$MTC08,$WH8A,$IG02&view=STUD_REAR34|FRONT34&model=ct

`size` is a quality ladder (not exact px). Measured:
  400 → 400×225, 720–1000 → 720×405, 1200–1600 → 1440×810, 1920 → 1920×1080
'''

# Fleet exterior_color name → Tesla paint option
EXTERIOR_COLOR_TO_CODE = {
    "solidblack": "PBSB",
    "black": "PBSB",
    "obsidianblack": "PMBL",
    "pearlwhite": "PPSW",
    "pearlwhitemulticoat": "PPSW",
    "white": "PPSW",
    "deepblue": "PPSB",
    "deepbluemetallic": "PPSB",
    "blue": "PPSB",
    "steelgrey": "PMNG",
    "midnightsilver": "PMNG",
    "midnightsilvermetallic": "PMNG",
    "grey": "PMNG",
    "gray": "PMNG",
    "silver": "PMSS",
    "silvermetallic": "PMSS",
    "redmulticoat": "PPMR",
    "red": "PPMR",
    "ultrared": "PR01",
    "stealthgrey": "PN00",
    "quicksilver": "PN01",
}

VALID_COMPOSITOR_COLORS = frozenset({
    "PBSB", "PPMR", "PMNG", "PPSB", "PPSW", "PMSS", "PMBL",
})

# Model 3 wheels: (legacy code, highland code)
WHEEL_TYPE_M3 = {
    "pinwheel18": ("W38B", "W38A"),
    "pinwheel18capkit": ("W38B", "W38A"),
    "aero18": ("W38B", "W38A"),
    "glider18": ("W38B", "W38A"),
    "photon18": ("W38B", "W38A"),
    "prismata18": ("W38B", "W38A"),
    "stiletto19": ("W39B", "W39B"),
    "sport19": ("W39B", "W39B"),
    "pinwheel19": ("W39B", "W39B"),
    "gemini19": ("W39B", "W39B"),
    "induction19": ("W39B", "W39B"),
    "performance": ("W32B", "W32B"),
    "uberaerodisc": ("W32B", "W32B"),
}

# Model 3 Highland-only wheel names from Fleet
HIGHLAND_M3_WHEELS = frozenset({"glider18", "photon18", "prismata18"})

# Model Y Juniper / refresh wheel hints (when present, use design_studio_2)
JUNIPER_MY_WHEELS = frozenset({
    "crossflow19", "helix19", "photon19", "uranus19",
})


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def map_color_code(color: str) -> str:
    """Normalize URL color segment (code or name) to a compositor paint code."""
    raw = (color or "").strip()
    if not raw:
        return "PPSW"
    upper = raw.upper().lstrip("$")
    if upper in VALID_COMPOSITOR_COLORS:
        return upper
    mapped = EXTERIOR_COLOR_TO_CODE.get(_norm(raw))
    if mapped and mapped in VALID_COMPOSITOR_COLORS:
        return mapped
    if mapped:
        return "PMNG" if "grey" in _norm(raw) or "gray" in _norm(raw) else "PPSW"
    return "PPSW"


def normalize_car_model(car_model: str) -> str:
    """Map Fleet car_type to short compositor model key."""
    m = (car_model or "model3").strip().lower().replace(" ", "").replace("_", "")
    aliases = {
        "model3": "m3",
        "m3": "m3",
        "modely": "my",
        "my": "my",
        "models": "ms",
        "models2": "ms",
        "ms": "ms",
        "modelx": "mx",
        "mx": "mx",
        "cybertruck": "ct",
        "ct": "ct",
    }
    return aliases.get(m, "m3")


def is_highland_m3(wheel: str) -> bool:
    return _norm(wheel) in HIGHLAND_M3_WHEELS


def is_juniper_my(wheel: str) -> bool:
    key = _norm(wheel)
    if key in JUNIPER_MY_WHEELS:
        return True
    # Newer Fleet names often include these tokens
    return any(t in key for t in ("crossflow", "helix", "juniper"))


def m3_wheel_codes(wheel: str) -> tuple[str, str]:
    key = _norm(wheel)
    if key in WHEEL_TYPE_M3:
        return WHEEL_TYPE_M3[key]
    if "18" in key:
        return "W38B", "W38A"
    if "20" in key:
        return "W32B", "W32B"
    return "W39B", "W39B"


def studio_url(
    *,
    model: str,
    options: list[str],
    view: str,
    size: str,
    bkba_opt: str = "1",
) -> str:
    """Build a design_studio_2 compositor URL (current Tesla design studio)."""
    opts = ",".join(f"${o.lstrip('$')}" for o in options)
    return (
        "https://static-assets.tesla.com/configurator/compositor?"
        f"context=design_studio_2&options={opts}"
        f"&view={view}&model={model}&size={size}"
        f"&bkba_opt={bkba_opt}&crop=0,0,0,0&overlay=0"
    )


def build_compositor_url(color: str, wheel: str, car_model: str, size: str = "1920") -> str:
    color_code = map_color_code(color)
    model = normalize_car_model(car_model)
    wheel_key = _norm(wheel)

    # --- Cybertruck ---
    if model == "ct":
        # Tesla studio uses $MTC08,$WH8A,$IG02 (stainless — no paint code)
        # FRONT34 reads better in our hero than STUD_REAR34
        return studio_url(
            model="ct",
            options=["MTC08", "WH8A", "IG02"],
            view="FRONT34",
            size=size,
            bkba_opt="1",
        )

    # --- Model S (refresh / Plaid era on design studio) ---
    if model == "ms":
        return studio_url(
            model="ms",
            options=["MTS14", color_code, "WS10", "ICC00"],
            view="FRONT34",
            size=size,
        )

    # --- Model X (refresh) ---
    if model == "mx":
        return studio_url(
            model="mx",
            options=["MTX13", color_code, "WX00", "IWW00"],
            view="FRONT34",
            size=size,
        )

    # --- Model Y ---
    if model == "my":
        if is_juniper_my(wheel):
            # Juniper / refresh (tesla.com design studio)
            # $MTY86,$PPSW,$WY19P,$IPB12&view=FRONT34&model=my
            y_wheel = "WY19P"
            if "20" in wheel_key:
                y_wheel = "WY20P"  # best-effort; falls back visually if unknown
            return studio_url(
                model="my",
                options=["MTY86", color_code, y_wheel, "IPB12"],
                view="FRONT34",
                size=size,
            )
        # Pre-refresh / classic Model Y (still works with legacy compositor)
        return (
            "https://static-assets.tesla.com/configurator/compositor?"
            f"&options=$WY19B,${color_code},$DV4W,$MTY03,$INYPB&view=STUD_3QTR&model=my"
            f"&size={size}&bkba_opt=1&version=v0027d202004163351"
        )

    # --- Model 3 ---
    legacy_wheel, highland_wheel = m3_wheel_codes(wheel)
    if is_highland_m3(wheel):
        # Highland: $MT370,$PPSW,$W38A,$IPB3&view=STUD_FRONT34&model=m3
        return studio_url(
            model="m3",
            options=["MT370", color_code, highland_wheel, "IPB3"],
            view="STUD_FRONT34",
            size=size,
        )
    # Pre-Highland Model 3
    return (
        "https://static-assets.tesla.com/configurator/compositor?"
        f"&options=${color_code},${legacy_wheel},$DV4W,$MT303,$IN3PB&view=STUD_3QTR&model=m3"
        f"&size={size}&bkba_opt=1&version=0.0.25"
    )


def _guess_content_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "application/octet-stream"


def _fetch_url(stored_url: str) -> str:
    """Strip our local cache tag; Tesla only sees the real compositor URL."""
    return (stored_url or "").split("#", 1)[0]


def _cache_key(compositor_url: str) -> str:
    return f"{compositor_url}#{_IMAGE_CACHE_TAG}"


def crop_empty_background(
    im: Image.Image,
    *,
    alpha_threshold: int = 28,
    padding: int = 16,
) -> Image.Image:
    """
    Tight-crop Tesla compositor frames.

    Renders are often 1920×1080 with the car only in the middle ~40–50% of the
    height (transparent padding). Threshold alpha so faint anti-aliased noise
    does not keep huge empty margins (plain getbbox is too greedy).
    """
    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    mask = alpha.point(lambda p: 255 if p >= alpha_threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return rgba

    left, top, right, bottom = bbox
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(rgba.size[0], right + padding)
    bottom = min(rgba.size[1], bottom + padding)

    # Avoid "cropping" to almost the same canvas (tiny gain not worth it)
    cropped = rgba.crop((left, top, right, bottom))
    full_area = rgba.size[0] * rgba.size[1]
    crop_area = cropped.size[0] * cropped.size[1]
    if full_area and crop_area / full_area > 0.92:
        return rgba
    return cropped


def CreateImageFile(image):
    """Download from Tesla, crop empty background, store PNG."""
    fetch_url = _fetch_url(image.image_url)
    raw = urlopen(fetch_url, timeout=60).read()
    im = Image.open(io.BytesIO(raw))
    cropped = crop_empty_background(im)

    buf = io.BytesIO()
    cropped.save(buf, format="PNG", optimize=True)
    buf.seek(0)

    img_temp = NamedTemporaryFile(delete=True, suffix=".png")
    img_temp.write(buf.read())
    img_temp.flush()
    image.image_file.save(f"image_{image.pk}.png", File(img_temp))
    image.save()


def CarImageFromTesla(request, color, wheel, CarModel):
    """Proxy / cache Tesla compositor render for the vehicle status page."""
    size = "1920"
    url = build_compositor_url(color, wheel, CarModel, size=size)
    cache_url = _cache_key(url)

    willNeedFileCreation = False
    try:
        try:
            image = TeslaImage.objects.get(image_url=cache_url)
        except ObjectDoesNotExist:
            image = TeslaImage()
            image.image_url = cache_url
            image.save()
            willNeedFileCreation = True
        if image.image_url and not image.image_file:
            willNeedFileCreation = True
        if willNeedFileCreation:
            CreateImageFile(image)
        try:
            data = image.image_file.read()
            image.image_file.seek(0)
            return HttpResponse(data, content_type=_guess_content_type(data))
        except Exception:
            CreateImageFile(image)
            data = image.image_file.read()
            return HttpResponse(data, content_type=_guess_content_type(data))
    except Exception:
        return HttpResponseRedirect(url)
