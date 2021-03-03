# proxy to use since someone in Tesla (grr) did block all cloud (ie amazon aws)
# requests the 10/9/2020. If prod, define HTTPS_PROXY. I found that
# proxies from https://proxy-seller.com works fine
# HTTPS_PROXY must be something like: http://user on proxy:pw on proxy@IP address:port
import os

def GetProxyToUse():
    try:
        proxyStr = os.environ['HTTPS_PROXY']
    except KeyError:
        return None
    if proxyStr is None:
        return None
    proxiesDict = {"https": proxyStr}
    return proxiesDict
