from matesla.units import context_for_unit, get_distance_unit
from mysite.writable_access import is_writable_request


def writable_access(request):
    return {"allow_writes": is_writable_request(request)}


def distance_unit(request):
    """Site-wide km/mi preference for templates."""
    unit = getattr(request, "distance_unit", None) or get_distance_unit(request)
    return context_for_unit(unit)


def geocoder_attribution(request):
    """
    Free-tier Geoapify requires a visible « Powered by Geoapify » link + OSM.
    Only when reverse-geocode actually uses Geoapify (API key present).
    """
    try:
        from matesla.models.AddressFromLatLong import active_geocoder

        use_geoapify = active_geocoder() == "geoapify"
    except Exception:
        use_geoapify = False
    return {"use_geoapify": use_geoapify}


# OSMF attribution guidelines: « © OpenStreetMap contributors » on the map,
# linking to the copyright page. Keep English — it is a licence notice.
OSM_TILE_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">'
    "OpenStreetMap</a> contributors"
)


def osm_tiles(request):
    """Leaflet raster URL + licence attribution (OSMF tile usage policy)."""
    from django.conf import settings

    return {
        "osm_tile_url": getattr(
            settings,
            "OSM_TILE_URL",
            "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        ),
        "osm_tile_attribution": OSM_TILE_ATTRIBUTION,
    }
