"""The public MicroObservatory image directory: what nights exist, and downloading their frames."""
import gzip
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import net
from .config import FRAME_BYTES, IMAGE_URL, LISTING_URL

_NAME = re.compile(r'^[A-Za-z0-9+_.-]{1,40}\d{12}$')      # target + YYMMDDhhmmss
ID_PATTERN = r'[a-z0-9]+-\d{8}'                          # what night_id() returns


@dataclass(frozen=True)
class Frame:
    name: str
    time: datetime          # UTC start of exposure
    target: str
    telescope: str
    exposure: float
    weather: int = -1       # the directory's own "% clear" figure; recorded, never trusted


def slug(text):
    return re.sub(r'[^a-z0-9]', '', text.lower())


def spaced(planet):
    """'WASP-50b' -> 'WASP-50 b' and 'WASP-77Ab' -> 'WASP-77 A b', the way planets are written."""
    return re.sub(r'(\d)([A-D]?)([b-z])$', lambda m: ' '.join(filter(None, m.groups())), planet)


def _cells(row):
    return [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', c).replace('&nbsp;', ' ')).strip()
            for c in re.findall(r'<td[^>]*>(.*?)</td>', row, flags=re.S)]


def parse_listing(html):
    frames = []
    for row in re.findall(r'<tr.*?</tr>', html, flags=re.S):
        m = re.search(r'openImageName=([^"&\'<> ]+)', row)
        if not m or not _NAME.match(m.group(1)):
            continue
        name, c = m.group(1), _cells(row)
        if len(c) < 9:
            continue
        try:
            when = datetime.strptime(name[-12:], '%y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            clear = re.search(r'(-?\d+)% Clear', row)
            frames.append(Frame(name, when, name[:-12], c[8], float(c[5]), int(clear.group(1)) if clear else -1))
        except ValueError:
            continue
    return frames


def listing(days):
    """Every frame listed for the last 10, 20 or 30 days: all targets, darks included. The directory's own
    "ExoPlanets" search is not used because it leaves out more than half of the planets observed."""
    return parse_listing(net.get(LISTING_URL % days).decode('utf-8', 'replace'))


def is_dark(frame):
    return frame.target.lower().startswith('dark')


def group_nights(frames, gap_hours=3):
    """Split each target's frames into nights; a gap longer than gap_hours starts a new one."""
    by_target = {}
    for f in frames:
        if not is_dark(f):
            by_target.setdefault((f.target, f.telescope), []).append(f)
    nights = []
    for run in by_target.values():
        run.sort(key=lambda f: f.time)
        night = [run[0]]
        for f in run[1:]:
            if f.time - night[-1].time > timedelta(hours=gap_hours):
                nights.append(night)
                night = []
            night.append(f)
        nights.append(night)
    return sorted(nights, key=lambda n: n[0].time)


def night_id(night, telescope=False):
    first = night[0]
    return '%s%s-%s' % (slug(first.target), slug(first.telescope) if telescope else '', first.time.strftime('%Y%m%d'))


def name_nights(nights):
    """{id: frames}. Two runs on one target in one day are one interrupted night if the telescope is
    the same; a second telescope gets its own id."""
    named = {}
    for night in nights:
        nid = night_id(night)
        if nid in named and named[nid][0].telescope != night[0].telescope:
            nid = night_id(night, telescope=True)
        named[nid] = named.get(nid, []) + night
    return named


def pick_darks(night, darks, count=2):
    """The darks of the same telescope and exposure closest in time to the night."""
    mid = night[0].time + (night[-1].time - night[0].time) / 2
    same = [d for d in darks if d.telescope == night[0].telescope and abs(d.exposure - night[0].exposure) < 0.01]
    return sorted(sorted(same, key=lambda d: abs(d.time - mid))[:count], key=lambda d: d.time)


def pick_sample(night, count):
    """The first frame plus frames spread evenly over the night."""
    if len(night) <= count:
        return list(night)
    step = (len(night) - 1) / (count - 1)
    return [night[round(i * step)] for i in range(count)]


def download(frame, folder, retries=3):
    """Save one frame gzipped in folder. Returns True if it was downloaded, False if already there."""
    if not _NAME.match(frame.name):
        raise ValueError('unexpected frame name: %r' % frame.name)
    dest = os.path.join(folder, frame.name + '.FITS.gz')
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return False
    error = None
    for attempt in range(retries):
        try:
            data = net.get(IMAGE_URL % frame.name)
            if len(data) != FRAME_BYTES or not data.startswith(b'SIMPLE'):
                raise IOError('not a MicroObservatory frame (%d bytes)' % len(data))
            with gzip.open(dest + '.part', 'wb', compresslevel=6) as fh:
                fh.write(data)
            os.replace(dest + '.part', dest)
            return True
        except OSError as exc:
            error = exc
            time.sleep(2 * (attempt + 1))
    kind = net.Unreachable if isinstance(error, net.Unreachable) else IOError
    raise kind('%s: %s' % (frame.name, error))
