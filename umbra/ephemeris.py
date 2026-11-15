"""Ephemerides and transit prediction for a night. ExoClock first; the NASA Exoplanet Archive for planets it lacks."""
import json
import time
import warnings
from urllib.parse import urlencode

import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time
from astropy.utils import iers
import astropy.units as u

from . import net
from .config import ALIASES, ARCHIVE_URL, EXOCLOCK_URL
from .directory import slug

iers.conf.auto_download = False         # never fetch Earth-orientation tables; the built-in ones are enough here
iers.conf.auto_max_age = None
iers.conf.iers_degraded_accuracy = 'ignore'  # predictions past the built-in table cost under a second
warnings.filterwarnings('ignore', message='Tried to get polar motions')

WHIPPLE = EarthLocation(lat=31.68 * u.deg, lon=-110.88 * u.deg, height=1268 * u.m)


ARCHIVE = 'NASA Exoplanet Archive'
_COLUMNS = ('pl_name', 'hostname', 'ra', 'dec', 'pl_orbper', 'pl_orbpererr1', 'pl_tranmid', 'pl_tranmiderr1', 'pl_trandur', 'pl_trandep',
            'pl_ratror', 'pl_ratdor', 'pl_orbsmax', 'st_rad', 'pl_orbincl', 'pl_orbeccen', 'pl_orblper', 'st_teff', 'st_tefferr1',
            'st_tefferr2', 'st_met', 'st_meterr1', 'st_meterr2', 'st_logg', 'st_loggerr1', 'st_loggerr2', 'sy_vmag')
_AU = 215.032           # one astronomical unit in solar radii


def _cached(cache, fetch, max_age_days):
    """What fetch() returns, kept on disk for a week. If the source cannot be reached, the old copy is used."""
    if not (cache.exists() and time.time() - cache.stat().st_mtime < max_age_days * 86400):
        try:
            table = json.loads(fetch())
            cache.write_text(json.dumps(table))
            return table
        except (IOError, ValueError):
            if not cache.exists():
                raise
    return json.loads(cache.read_text())


def load(cache, max_age_days=7):
    """The ExoClock planet table."""
    return _cached(cache, lambda: net.get(EXOCLOCK_URL), max_age_days)


def load_archive(cache, max_age_days=7):
    """Planet b of every transiting system in the NASA Exoplanet Archive."""
    query = "select %s from pscomppars where tran_flag=1 and pl_letter='b'" % ','.join(_COLUMNS)
    return _cached(cache, lambda: net.get(ARCHIVE_URL + '?' + urlencode(dict(query=query, format='json'))), max_age_days)


def lookup(target, table):
    """The ExoClock record of planet b of a directory target, or None."""
    key = slug(target)
    if key in ALIASES:
        return table.get(ALIASES[key])
    return next((v for k, v in table.items() if slug(k) == key + 'b'), None)


def from_archive(target, rows):
    """Planet b of a directory target from the archive's table, shaped like an ExoClock record. None when the
    planet is not there or the archive lacks a number the prediction or EXOTIC needs."""
    r = next((r for r in rows if slug(r['hostname']) == slug(target)), None)
    if not r:
        return None
    depth = r['pl_trandep'] / 100 if r['pl_trandep'] else (r['pl_ratror'] or 0) ** 2
    distance = r['pl_ratdor'] or (r['pl_orbsmax'] * _AU / r['st_rad'] if r['pl_orbsmax'] and r['st_rad'] else None)
    if not all((r['pl_orbper'], r['pl_tranmid'], r['pl_trandur'], 0 < depth < 1, distance, r['pl_orbincl'], r['st_teff'], r['st_logg'])):
        return None
    where = SkyCoord(r['ra'] * u.deg, r['dec'] * u.deg)
    return dict(source=ARCHIVE, name=r['pl_name'].replace(' ', ''), star=r['hostname'],
                ra_j2000=str(where.ra.to_string(u.hourangle, sep=':', precision=2, pad=True)),
                dec_j2000=str(where.dec.to_string(sep=':', precision=1, alwayssign=True, pad=True)),
                ephem_mid_time=r['pl_tranmid'], ephem_mid_time_e1=r['pl_tranmiderr1'],
                ephem_period=r['pl_orbper'], ephem_period_e1=r['pl_orbpererr1'],
                duration_hours=r['pl_trandur'], depth_r_mmag=float(-2500 * np.log10(1 - depth)),
                rp_over_rs=r['pl_ratror'] or round(float(np.sqrt(depth)), 4), sma_over_rs=round(distance, 3),
                inclination=r['pl_orbincl'], eccentricity=r['pl_orbeccen'], periastron=r['pl_orblper'],
                teff=r['st_teff'], teff_e1=r['st_tefferr1'], teff_e2=r['st_tefferr2'],
                meta=r['st_met'], meta_e1=r['st_meterr1'], meta_e2=r['st_meterr2'],
                logg=r['st_logg'], logg_e1=r['st_loggerr1'], logg_e2=r['st_loggerr2'], v_mag=r['sy_vmag'])


def coordinates(entry):
    return SkyCoord(entry['ra_j2000'], entry['dec_j2000'], unit=(u.hourangle, u.deg))


def impact(entry):
    """Impact parameter b = (a / Rs) cos i, or None when the record lacks the numbers."""
    a, inc = entry.get('sma_over_rs'), entry.get('inclination')
    return round(float(a * np.cos(np.radians(inc))), 3) if a and inc else None


def bjd_tdb(times_utc, target, location=WHIPPLE):
    t = Time(times_utc, scale='utc', location=location)
    return (t.tdb + t.light_travel_time(target)).jd, t


def airmass(t, target, location=WHIPPLE):
    alt = target.transform_to(AltAz(obstime=t, location=location)).alt.deg
    return 1 / np.sin(np.radians(np.clip(alt, 1, 90)))


def predict(mid_times_utc, entry, location=WHIPPLE):
    """The transit expected in a night, from its frame mid-times (naive UTC datetimes, in time order).
    Returns the prediction, plus BJD_TDB and airmass per frame."""
    target = coordinates(entry)
    bjd, t = bjd_tdb(mid_times_utc, target, location)
    corr = float(np.mean(bjd - t.jd))
    t0, period, hours = entry['ephem_mid_time'], entry['ephem_period'], entry['duration_hours']
    epoch = round((float(np.mean(bjd)) - t0) / period)
    tc = t0 + epoch * period
    ingress, egress = tc - hours / 48, tc + hours / 48
    am = airmass(t, target, location)
    iso = lambda b: Time(b - corr, format='jd', scale='utc').isot[:19] + 'Z'
    transit = dict(tc_bjd=tc, ingress_bjd=ingress, egress_bjd=egress,
                   ingress=iso(ingress), mid=iso(tc), egress=iso(egress), duration_h=hours,
                   depth=1 - 10 ** (-entry['depth_r_mmag'] / 2500), v_mag=entry.get('v_mag'),
                   rp_rs=entry.get('rp_over_rs'), impact=impact(entry), teff=entry.get('teff'),
                   source=entry.get('source', 'ExoClock'),
                   pre_min=float((ingress - bjd[0]) * 1440), post_min=float((bjd[-1] - egress) * 1440),
                   airmass=[float(am[0]), float(am[-1]), float(am.max())])
    return transit, bjd, am
