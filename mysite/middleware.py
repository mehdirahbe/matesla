from mysite.writable_access import (
    is_readonly_safe_post,
    is_writable_request,
    readonly_post_forbidden_response,
    reject_readonly_remote_access,
)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ReadOnlyRemoteMiddleware:
    """
    Tailscale / remote Host → read-only: only whitelisted views, no Tesla
    account setup. Local 127.0.0.1 / localhost stay fully writable.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        denied = reject_readonly_remote_access(request)
        if denied is not None:
            return denied

        if (
            request.method in _MUTATING_METHODS
            and not is_writable_request(request)
            and not is_readonly_safe_post(request)
        ):
            return readonly_post_forbidden_response(request)
        return self.get_response(request)
