from matesla.units import context_for_unit, get_distance_unit
from mysite.writable_access import is_writable_request


def writable_access(request):
    return {"allow_writes": is_writable_request(request)}


def distance_unit(request):
    """Site-wide km/mi preference for templates."""
    unit = getattr(request, "distance_unit", None) or get_distance_unit(request)
    return context_for_unit(unit)
