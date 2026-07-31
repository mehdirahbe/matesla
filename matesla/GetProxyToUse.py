"""
Optional HTTPS proxy for outbound Tesla API calls.

Tesla blocked many cloud egress IPs (AWS, etc.) around 2020-10. Production can
set HTTPS_PROXY (e.g. from proxy-seller) so Fleet requests leave via a residential
or otherwise allowed address. Format:
  http://user:password@host:port
"""

import os


def GetProxyToUse():
    """
    Return a requests-style proxies dict for HTTPS, or None if unset.

    Example return: {"https": "http://user:pass@1.2.3.4:8080"}
    """
    try:
        proxy_url = os.environ["HTTPS_PROXY"]
    except KeyError:
        return None
    if proxy_url is None:
        return None
    return {"https": proxy_url}
