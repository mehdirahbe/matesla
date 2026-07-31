"""
Writable vs read-only access by Host (same idea as PicturesDjango).

Local (127.0.0.1 / localhost): full control — Tesla setup, admin.
Remote (Tailscale HTTPS hostname, etc.): login + browse only.
"""

from __future__ import annotations

import logging
import os
import sys

from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.urls import Resolver404, resolve
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

_DEFAULT_WRITABLE_HOSTS = ("127.0.0.1", "localhost")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _writable_hosts():
    hosts = {
        host.lower()
        for host in getattr(settings, "WRITABLE_HOSTS", _DEFAULT_WRITABLE_HOSTS)
    }
    # Django test client uses host "testserver"
    if "test" in sys.argv or getattr(settings, "TESTING", False):
        hosts.add("testserver")
    return hosts


def request_host(request) -> str:
    return request.get_host().split(":")[0].lower()


def is_writable_request(request) -> bool:
    return request_host(request) in _writable_hosts()


def is_admin_request(request) -> bool:
    return "/admin" in request.path_info


def _readonly_allowed_url_names():
    return frozenset(getattr(settings, "READONLY_ALLOWED_URL_NAMES", ()))


def reject_readonly_remote_access(request):
    """
    On remote hosts, allow only whitelisted url names; everything else → 404
    (including admin and Tesla setup URLs).
    """
    if is_writable_request(request):
        return None

    if is_admin_request(request):
        logger.warning(
            "Blocked admin access on read-only host %s path=%s",
            request.get_host(),
            request.path,
        )
        return HttpResponseNotFound()

    try:
        match = resolve(request.path_info)
    except Resolver404:
        return None

    if match.url_name in _readonly_allowed_url_names():
        return None

    logger.warning(
        "Blocked non-whitelisted access on read-only host %s path=%s url_name=%s",
        request.get_host(),
        request.path,
        match.url_name,
    )
    return HttpResponseNotFound()


def is_readonly_safe_post(request) -> bool:
    if request.method not in _MUTATING_METHODS:
        return False
    try:
        match = resolve(request.path_info)
    except Resolver404:
        return False
    safe_names = getattr(settings, "READONLY_SAFE_POST_URL_NAMES", frozenset())
    return match.url_name in safe_names


def readonly_post_forbidden_response(request):
    logger.warning(
        "Blocked mutating %s on read-only host %s path=%s",
        request.method,
        request.get_host(),
        request.path,
    )
    return HttpResponseForbidden(
        _("Write operations are only allowed from the local server."),
        content_type="text/plain",
    )


def reject_readonly_post(request):
    if request.method not in _MUTATING_METHODS:
        return None
    if is_writable_request(request) or is_readonly_safe_post(request):
        return None
    return readonly_post_forbidden_response(request)


def get_site_owner_user():
    """
    Single household owner: the Django user that holds the Tesla token.

    Personal install is not multi-tenant. On Tailscale read-only, anonymous
    viewers act as this owner for *reads* only (token / vehicle list).
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    owner_id = getattr(settings, "MATESLA_OWNER_USER_ID", None)
    if owner_id:
        user = User.objects.filter(pk=owner_id).first()
        if user:
            return user
    owner_name = getattr(settings, "MATESLA_OWNER_USERNAME", None) or os.environ.get(
        "MATESLA_OWNER_USERNAME"
    )
    if owner_name:
        user = User.objects.filter(username=owner_name).first()
        if user:
            return user

    # Prefer the user that actually has a TeslaToken
    try:
        from matesla.models.TeslaToken import TeslaToken

        user_ids = list(
            TeslaToken.objects.order_by("user_id")
            .values_list("user_id", flat=True)
            .distinct()[:2]
        )
        if len(user_ids) == 1:
            return User.objects.filter(pk=user_ids[0]).first()
        if len(user_ids) > 1:
            logger.warning(
                "Multiple TeslaToken owners %s — set MATESLA_OWNER_USER_ID",
                user_ids,
            )
            return User.objects.filter(pk=user_ids[0]).first()
    except Exception:
        logger.exception("get_site_owner_user: TeslaToken lookup failed")

    user = User.objects.filter(is_superuser=True).order_by("id").first()
    if user:
        return user
    return User.objects.order_by("id").first()


def resolve_acting_user(request):
    """
    User for data access:
    - logged-in session → that user
    - read-only remote (Tailscale) without login → site owner (household view)
    - writable local without login → None (must sign in)
    """
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    # Fallback when AuthenticationMiddleware has not run (tests / edge)
    if hasattr(request, "session"):
        try:
            from django.contrib.auth import get_user

            user = get_user(request)
            if getattr(user, "is_authenticated", False):
                return user
        except Exception:
            pass
    if not is_writable_request(request):
        return get_site_owner_user()
    return None
