"""All network access goes through here: HTTPS only, allow-listed hosts only, no redirects, and a server that says
it is busy is left alone for a while."""
import json
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from . import __version__
from .config import ALLOWED_HOSTS, HOME_URL

# what this is and where to reach its author, for the logs of the servers it reads
_UA = {'User-Agent': '%s/%s (+%s; Exoplanet Watch)' % (HOME_URL.rsplit('/', 1)[1], __version__, HOME_URL)}


def _check(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError('blocked URL: %s' % url)


class Unreachable(IOError):
    """No connection, or the server is failing. Nothing is wrong with the night: try again later."""


class Busy(Unreachable):
    """The server is overloaded or failing. It is not asked again before the pause is over."""


_FIRST, _LONGEST = 600, 86400       # the pause after a busy answer: ten minutes, doubled each time, up to a day
_paused = {}                        # host -> (when it may be asked again, busy answers in a row)


def _asked(resp):
    """The seconds a Retry-After header asks for, given as a number or as a date; 0 without one."""
    value = resp.headers.get('Retry-After', '').strip()
    try:
        return max(0.0, float(value) if value.isdigit() else parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return 0.0


def _back_off(host, why, asked=0.0):
    row = _paused.get(host, (0, 0))[1]
    pause = max(asked, min(_LONGEST, _FIRST * 2 ** row))
    _paused[host] = (time.time() + pause, row + 1)
    raise Busy('%s from %s, not asked again for %d min' % (why, host, round(pause / 60)))


def _fetch(method, url, timeout, **extra):
    _check(url)
    host = urlsplit(url).hostname
    if time.time() < _paused.get(host, (0, 0))[0]:
        raise Busy('%s is being left alone for now' % host)
    try:
        resp = requests.request(method, url, headers=_UA, timeout=timeout, allow_redirects=False, **extra)
    except requests.Timeout:
        _back_off(host, 'no answer')
    except requests.RequestException as exc:
        raise Unreachable('%s, %s' % (host, type(exc).__name__)) from None
    if resp.status_code == 429 or resp.status_code >= 500:
        _back_off(host, 'HTTP %s' % resp.status_code, _asked(resp))
    _paused.pop(host, None)
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
