from mysite.writable_access import is_writable_request


def writable_access(request):
    return {"allow_writes": is_writable_request(request)}
