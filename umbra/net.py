"""All network access goes through here: HTTPS only, allow-listed hosts only, no redirects."""
import json
from urllib.parse import urlsplit

import requests

from .config import ALLOWED_HOSTS, SLUG

_UA = {'User-Agent': '%s (Exoplanet Watch participant)' % SLUG}


def _check(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError('blocked URL: %s' % url)


class Unreachable(IOError):
    """No connection, or the server is failing. Nothing is wrong with the night: try again later."""


def _fetch(method, url, timeout, **extra):
    _check(url)
    host = urlsplit(url).hostname
    try:
        resp = requests.request(method, url, headers=_UA, timeout=timeout, allow_redirects=False, **extra)
    except requests.RequestException as exc:
        raise Unreachable('%s, %s' % (host, type(exc).__name__)) from None
    if resp.status_code >= 500:
        raise Unreachable('HTTP %s from %s' % (resp.status_code, host))
    if resp.is_redirect or resp.status_code != 200:
        raise IOError('HTTP %s from %s' % (resp.status_code, host))
    want = resp.headers.get('Content-Length')
    if want and not resp.headers.get('Content-Encoding') and int(want) != len(resp.content):
        raise Unreachable('short read from %s' % host)
    return resp.content


def get(url, timeout=120):
    return _fetch('GET', url, timeout)


def post_json(url, request, timeout=120):
    """POST a MAST-style request and return the decoded JSON reply."""
    return json.loads(_fetch('POST', url, timeout, data={'request': json.dumps(request)}))
