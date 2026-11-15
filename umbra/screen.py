"""Screen one night: sky, coverage, gaps, and a quick ensemble light curve when the night is worth one.

The result is a plain dict (JSON-ready). grade.py turns it into a verdict.
"""
import glob
import json
import os

import numpy as np
from astropy.coordinates import EarthLocation
from astropy.io import fits
import astropy.units as u

from . import ephemeris, net, photometry as ph
from .config import MAST_URL

RULES = 7       # raise when screening changes: lost nights still on disk are then screened again


def read_frames(folder):
    """(mid-exposure Time, header, image, file name) for every frame in folder, in time order."""
    frames = []
    for path in glob.glob(os.path.join(folder, '*.FITS.gz')):
        try:
            with fits.open(path) as hdul:
                header, data = hdul[0].header, hdul[0].data.astype(np.float32)
            when = ph.frame_time(header)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):     # damaged on disk: counts as a missing frame
            continue
        frames.append((when, header, data, os.path.basename(path)))
    return sorted(frames, key=lambda f: f[0].jd)


def site(header):
    return EarthLocation(lat=float(header.get('LATITUDE', 31.68)) * u.deg,
                         lon=-abs(float(header.get('LONGITUD', 110.88))) * u.deg,     # Whipple is west
                         height=float(header.get('HEIGHT', 1268)) * u.m)


def sky_state(stars, min_stars):
    """From star counts: transparency per frame (0-1), the clouded mask, and whether the whole night is overcast."""
    level = float(np.percentile(stars, 90))             # the median fails on mostly cloudy nights
    overcast = level < min_stars                        # even the best frames show almost nothing
    clear = np.clip(stars / max(level, 1.0), 0, 1)
    return clear, (stars < 0.6 * level) | overcast, overcast


def tic_cone(ra, dec, radius, cache):
    """TESS Input Catalog stars around a position, cached per night."""
    if cache and os.path.exists(cache):
        with open(cache) as fh:
            return json.load(fh)
    reply = net.post_json(MAST_URL, {'service': 'Mast.Catalogs.Tic.Cone', 'format': 'json', 'pagesize': 20000,
                                     'page': 1, 'params': {'ra': ra, 'dec': dec, 'radius': radius}})
    rows = [{k: q.get(k) for k in ('ID', 'ra', 'dec', 'Tmag', 'Vmag', 'Bmag')} for q in reply['data']]
    if cache:
        with open(cache, 'w') as fh:
            json.dump(rows, fh)
    return rows


def screen(folder, entry, min_stars=40, deep=True):
    """Measure a night. entry is the planet's ExoClock record. deep=False skips the light curve."""
    frames = read_frames(folder)
    if not frames:
        raise ValueError('no frames in %s' % folder)
    header = frames[0][1]
    eph, bjd, airmass = ephemeris.predict([f[0].to_datetime() for f in frames], entry, site(header))
    jd = np.array([f[0].jd for f in frames])
    cadence = float(np.median(np.diff(jd)) * 1440) if len(frames) > 1 else 0.0

    preps = [ph.prep(f[2]) for f in frames]
    stars = np.array([ph.detect(p, 5)[1] for p in preps])
    clear, cloudy, overcast = sky_state(stars, min_stars)
    ref = reference(stars, cloudy)
    dx, dy, locked = _drift(preps, ref)
    damaged = ~cloudy & ~locked                         # stars, but not this field: a failed readout
    cloudy, clear = cloudy | damaged, np.where(damaged, 0, clear)      # left out like a clouded frame

    ingress, egress = eph['ingress_bjd'], eph['egress_bjd']
    edge = 0.25 * eph['duration_h'] / 48
    in_transit = (bjd > ingress + edge) & (bjd < egress - edge)
    steps = np.diff(bjd) * 1440
    lost = [max(0.0, (min(bjd[i + 1], egress) - max(bjd[i], ingress)) * 1440)
            for i in range(len(steps)) if steps[i] > 2.5 * cadence]

    result = dict(
        rules=RULES, frames=len(frames), exposure=float(header.get('EXPTIME', 0)), cadence_min=round(cadence, 1),
        telescope=str(header.get('TELESCOP', '')).strip(),
        binning='%dx%d' % (header.get('XBINNING', 2), header.get('YBINNING', 2)), transit=eph,
        sky=dict(overcast=bool(overcast), clouded=int(cloudy.sum()), damaged=int(damaged.sum()),
                 in_transit=int(in_transit.sum()), clouded_in_transit=int((cloudy & in_transit).sum()),
                 first_clear=not bool(cloudy[0]),
                 clear_pre_min=round(float(((bjd < ingress) & ~cloudy).sum() * cadence)),
                 clear_post_min=round(float(((bjd > egress) & ~cloudy).sum() * cadence))),
        gap=dict(in_transit_min=round(float(sum(lost)), 1)),
        series=dict(t=[f[0].isot[:19] + 'Z' for f in frames], file=[f[3] for f in frames], airmass=np.round(airmass, 3).tolist(),
                    clear=np.round(clear, 3).tolist(), cloudy=cloudy.tolist(),
                    in_transit=in_transit.tolist()),
        phot=None)

    hopeless = (in_transit.sum() == 0 or result['sky']['clouded_in_transit'] > 0.5 * in_transit.sum()
                or max(result['sky']['clear_pre_min'], result['sky']['clear_post_min']) < 30)
    if deep and not hopeless:
        result['phot'] = light_curve(frames, preps[ref], ref, dx, dy, bjd, cloudy, in_transit, eph, entry,
                                     os.path.join(folder, 'tic.json'))
    return result


def reference(stars, cloudy):
    """The frame every other is compared with: a typical clear one. The frame with the most stars can be damaged."""
    clear = np.nonzero(~cloudy)[0] if not cloudy.all() else np.arange(len(stars))
    return int(clear[np.argsort(stars[clear])[len(clear) // 2]])


def steady(flux, peaks, clear):
    """Which comparison stars are measurable and unsaturated in every clear frame. Clouded frames do not count."""
    f, p = flux[clear], peaks[clear]
    return np.all(np.isfinite(f) & (f > 0), axis=0) & (np.where(np.isfinite(p), p, np.inf).max(axis=0) < 0.85 * ph.SAT_ADU)


def flatten(rel, t, oot, in_transit, use):
    """The light curve with its out-of-transit level at 1 and a linear trend removed, and the points still used.
    Outliers are dropped on the way: one cosmic ray or satellite on the star would tilt the baseline."""
    for _ in range(3):
        base = oot & use
        flat = rel / np.polyval(np.polyfit(t[base], rel[base], 1), t)
        sigma = 1.4826 * np.nanmedian(abs(flat[base] - 1))
        inside = in_transit & use
        floor = min(1.0, np.nanmedian(flat[inside])) if inside.any() else 1.0
        use = use & (flat < 1 + 4 * sigma) & (flat > np.where(in_transit, floor, 1.0) - 4 * sigma)
    return flat, use


def _drift(preps, ref):
    """Whole-pixel drift of every frame against the reference, and which frames locked onto it.
    Shifts that did not lock are interpolated in time."""
    shifts = [ph.shift(p, preps[ref]) for p in preps]
    dx, dy = (np.array([s[k] for s in shifts], float) for k in (0, 1))
    locked = np.array([s[2] for s in shifts]) >= 8
    if locked.any() and not locked.all():                   # on an overcast night nothing locks, not even the reference
        for axis in (dx, dy):
            axis[~locked] = np.interp(np.nonzero(~locked)[0], np.nonzero(locked)[0], axis[locked])
    return dx, dy, locked


def _candidates(catalog, target, v_mag, colour, xy, tx, ty, on_chip):
    """Up to twelve comparison candidates: close to the target in brightness and colour, unblended, and
    on the chip all night. xy holds the pixel position of every catalog star."""
    tmag = np.array([c['Tmag'] if c['Tmag'] is not None else 99 for c in catalog])
    found = []
    for k, c in enumerate(catalog):
        if c['Vmag'] is None or c is target or not (v_mag - 1.5 < c['Vmag'] < v_mag + 1.2):
            continue
        bv = (c['Bmag'] - c['Vmag']) if c['Bmag'] else None
        if colour is not None and (bv is None or abs(bv - colour) > 0.35):
            continue
        x, y = xy[k]
        dist = float(np.hypot(x - tx, y - ty))
        if not on_chip(x, y) or dist < 25:
            continue
        near = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        near[k] = 99
        if np.any((near < 5) & (tmag < tmag[k] + 3)):       # a neighbour bright enough to spoil the aperture
            continue
        found.append(dict(x=float(x), y=float(y), V=c['Vmag'], bv=bv, dist=dist))
    found.sort(key=lambda c: abs(c['V'] - v_mag) + (abs(c['bv'] - colour) if colour is not None else 0) + c['dist'] / 500)
    return found[:12]


def light_curve(frames, prep, ref, dx, dy, bjd, cloudy, in_transit, eph, entry, cache):
    """Ensemble aperture photometry of the target against comparison stars chosen from the catalog.
    The field is recognized on the reference frame (prep is its cleaned image) and pixel positions are its;
    dx, dy carry them to the other frames.
    Returns the numbers the grade needs, or a dict with 'error' naming what stopped it."""
    header, images = frames[ref][1], [f[2] for f in frames]
    ny, nx = images[0].shape
    where = ephemeris.coordinates(entry)
    ra0, dec0 = float(header.get('RA', where.ra.deg)), float(header.get('DEC', where.dec.deg))
    catalog = [c for c in tic_cone(ra0, dec0, 0.6, cache) if c['ra'] is not None]
    stars, _ = ph.detect(prep, 60)
    sol = ph.plate_solve(stars, catalog, ra0, dec0, (ny, nx)) if len(stars) >= 6 and catalog else None
    if sol is None or sol['nmatch'] < 12 or sol['rms'] > 1.5:
        return dict(error='plate')
    topix = sol['topix']
    tx, ty = topix(where.ra.deg, where.dec.deg)
    target = min(catalog, key=lambda c: (c['ra'] - where.ra.deg) ** 2 * np.cos(where.dec.rad) ** 2 + (c['dec'] - where.dec.deg) ** 2)
    v_mag = target['Vmag'] or target['Tmag']
    colour = (target['Bmag'] - target['Vmag']) if (target['Bmag'] and target['Vmag']) else None

    def on_chip(x, y):
        X, Y = x + dx, y + dy
        return bool(np.all((X > ph.EDGE) & (X < nx - ph.EDGE) & (Y > ph.EDGE) & (Y < ny - ph.EDGE)))

    measure = lambda x, y: [ph.phot(image, x + a, y + b) for image, a, b in zip(images, dx, dy)]
    flux, peaks = np.array(measure(tx, ty)).T               # the target: one value per frame
    clear = ~cloudy if (~cloudy).any() else np.ones(len(cloudy), bool)
    if not np.isfinite(peaks[clear]).any():                 # the solution puts the target off the chip
        return dict(error='plate')
    peak = float(np.nanmax(peaks[clear]))
    base = dict(x=round(tx, 1), y=round(ty, 1), v_mag=v_mag, peak=round(peak), plate_matched=sol['nmatch'],
                plate_rms=round(sol['rms'], 2), drift=[int(dx.min()), int(dx.max()), int(dy.min()), int(dy.max())],
                dx=[int(v) for v in dx], dy=[int(v) for v in dy])
    if v_mag is None:
        return dict(base, error='comparisons')
    xy = np.array([topix(c['ra'], c['dec']) for c in catalog])
    comps = _candidates(catalog, target, v_mag, colour, xy, tx, ty, on_chip)
    if not comps:
        return dict(base, error='comparisons')
    both = np.array([measure(c['x'], c['y']) for c in comps])       # star x frame x (flux, peak)
    comp_flux, comp_peak = both[:, :, 0].T, both[:, :, 1].T         # frame x star
    keep = np.nonzero(steady(comp_flux, comp_peak, clear))[0]       # clouded frames must not disqualify a star
    if len(keep) < 2:
        return dict(base, error='comparisons')

    ingress, egress, tc = eph['ingress_bjd'], eph['egress_bjd'], eph['tc_bjd']
    use = ~cloudy & np.isfinite(flux)
    oot = ((bjd < ingress - 0.003) | (bjd > egress + 0.003)) & use
    if oot.sum() < 6:
        return dict(base, error='baseline')
    norm = comp_flux[:, keep] / np.nanmedian(comp_flux[oot][:, keep], axis=0)
    spread = np.nanstd(norm[use] / np.nanmedian(norm, axis=1)[use, None], axis=0)
    steadiest = np.argsort(spread)                          # EXOTIC is offered the first five
    keep = keep[steadiest][spread[steadiest] < 2 * np.median(spread)]
    with np.errstate(invalid='ignore', divide='ignore'):   # a blank frame measures 0 / 0; it is not used
        rel = flux / comp_flux[:, keep].sum(axis=1)
    flat, use = flatten(rel, bjd - tc, oot, in_transit, use)
    oot, inside = oot & use, in_transit & use
    scatter = float(np.nanstd(flat[oot]))
    out = dict(base, comps=[[round(comps[k]['x']), round(comps[k]['y'])] for k in keep], scatter=round(scatter, 4),
               saturated=peak >= 0.95 * ph.SAT_ADU,
               rel=[round(float(v), 4) if ok and np.isfinite(v) else None for v, ok in zip(flat, use)])
    if inside.sum():
        depth = float(np.nanmean(flat[oot]) - np.nanmean(flat[inside]))
        err = float(np.hypot(scatter / np.sqrt(oot.sum()), np.nanstd(flat[inside]) / np.sqrt(inside.sum())))
        out.update(depth=round(depth, 4), depth_err=round(err, 4), snr=round(depth / err, 1) if err > 0 else 0.0)
    return out
