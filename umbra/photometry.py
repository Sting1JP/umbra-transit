"""Frame-level photometry and plate solving for MicroObservatory images (650 x 500, about 5.2 arcsec/px)."""
from datetime import datetime, timezone

import numpy as np
from scipy import ndimage
from astropy.time import Time
import astropy.units as u

SAT_ADU = 4095          # 12-bit camera
APER, ANN_IN, ANN_OUT = 3.5, 7.0, 12.0      # aperture and sky annulus radii, px
HALF = 14               # half-size of the cutout photometry works on
EDGE = HALF + 2         # a star must stay this far from the edge in every frame


def frame_time(header):
    """Mid-exposure time (UTC). DATE-OBS has ms precision and a timezone; MJD-OBS only 0.001 d."""
    exposure = float(header.get('EXPTIME', 0) or 0)
    try:
        start = datetime.strptime(header.get('DATE-OBS'), '%Y-%m-%dT%H:%M:%S.%f%z').astimezone(timezone.utc)
        t = Time(start.replace(tzinfo=None), scale='utc')
    except (TypeError, ValueError):
        t = Time(header['MJD-OBS'], format='mjd', scale='utc')
    return t + exposure / 2 * u.s


def background(image, box=32):
    """Smooth background from block medians (much faster than a large median filter)."""
    ny, nx = image.shape
    padded = np.pad(image, ((0, (-ny) % box), (0, (-nx) % box)), mode='edge')
    blocks = np.median(padded.reshape(padded.shape[0] // box, box, padded.shape[1] // box, box), axis=(1, 3))
    return ndimage.zoom(ndimage.median_filter(blocks, 3), box, order=1)[:ny, :nx]


def prep(image):
    """The frame without hot pixels, cosmic rays or background, ready for star detection."""
    clean = ndimage.median_filter(image, 3)
    return clean - background(clean)


def detect(image, count=60):
    """The brightest stars as rows of 1-based (x, y, flux), and how many peaks stood out in all."""
    smooth = ndimage.gaussian_filter(image, 1.2)
    noise = 1.4826 * np.median(abs(smooth - np.median(smooth)))
    peaks = (smooth == ndimage.maximum_filter(smooth, 9)) & (smooth > 8 * noise)
    yy, xx = np.mgrid[-4:5, -4:5]
    stars = []
    for y, x in zip(*np.nonzero(peaks)):
        if x < 8 or y < 8 or x > image.shape[1] - 9 or y > image.shape[0] - 9:
            continue
        box = np.clip(image[y - 4:y + 5, x - 4:x + 5], 0, None)
        flux = box.sum()
        if flux > 0:
            stars.append((x + (box * xx).sum() / flux + 1, y + (box * yy).sum() / flux + 1, flux))
    stars.sort(key=lambda s: -s[2])
    return np.array(stars[:count]), int(peaks.sum())


def shift(frame, reference):
    """Whole-pixel (dx, dy) of frame against reference by cross-correlation, and how sharp the match is."""
    ny, nx = frame.shape
    a, b = np.fft.fft2(np.clip(frame, 0, 80)), np.fft.fft2(np.clip(reference, 0, 80))
    cc = np.fft.ifft2(a * np.conj(b)).real
    i, j = np.unravel_index(cc.argmax(), cc.shape)
    spread = cc.std()
    return (j if j < nx // 2 else j - nx), (i if i < ny // 2 else i - ny), ((cc.max() - cc.mean()) / spread if spread else 0.0)


def phot(image, x0, y0):
    """Aperture flux and peak value at 1-based (x0, y0), recentred on the star. NaN if off the chip."""
    ny, nx = image.shape
    xi, yi = int(round(x0)) - 1, int(round(y0)) - 1
    if not (HALF < xi < nx - HALF - 1 and HALF < yi < ny - HALF - 1):
        return np.nan, np.nan
    cut = image[yi - HALF:yi + HALF + 1, xi - HALF:xi + HALF + 1]
    yy, xx = np.mgrid[-HALF:HALF + 1, -HALF:HALF + 1] + 0.0
    start = cx, cy = x0 - 1 - xi, y0 - 1 - yi
    for _ in range(3):                                      # follow the centroid, but never further than 3 px
        r = np.hypot(xx - cx, yy - cy)
        light = np.clip((cut - np.median(cut[(r > ANN_IN) & (r < ANN_OUT)])) * (r < APER), 0, None)
        if light.sum() > 0:
            mx, my = (light * xx).sum() / light.sum(), (light * yy).sum() / light.sum()
            if np.hypot(mx - start[0], my - start[1]) < 3:
                cx, cy = mx, my
    r = np.hypot(xx - cx, yy - cy)
    sky = np.median(cut[(r > ANN_IN) & (r < ANN_OUT)])
    return (cut[r < APER] - sky).sum(), cut[r < APER + 1].max()


def tangent(ra, dec, ra0, dec0):
    """Tangent-plane offsets (arcsec) of ra, dec from the field centre ra0, dec0; all in degrees."""
    ra, dec, ra0, dec0 = map(np.radians, (np.asarray(ra), np.asarray(dec), ra0, dec0))
    c = np.sin(dec0) * np.sin(dec) + np.cos(dec0) * np.cos(dec) * np.cos(ra - ra0)
    xi = np.cos(dec) * np.sin(ra - ra0) / c
    eta = (np.cos(dec0) * np.sin(dec) - np.sin(dec0) * np.cos(dec) * np.cos(ra - ra0)) / c
    return np.degrees(xi) * 3600, np.degrees(eta) * 3600


def _rotation(degrees):
    t = np.radians(degrees)
    return np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])


def _matches(stars, x, y, tolerance):
    """(star index, catalog index) for every detected star with a catalog position within tolerance px."""
    apart = np.hypot(stars[:, None, 0] - x[None, :], stars[:, None, 1] - y[None, :])
    return [(i, apart[i].argmin()) for i in range(len(stars)) if apart[i].min() < tolerance]


def plate_solve(stars, catalog, ra0, dec0, shape):
    """Match detected stars (1-based x, y) to the catalog. Returns the sky-to-pixel function and the fit
    quality, or None when the field is not recognized."""
    ny, nx = shape
    bright = sorted([c for c in catalog if c['Tmag'] is not None], key=lambda c: c['Tmag'])[:100]
    sky = np.array(tangent([c['ra'] for c in bright], [c['dec'] for c in bright], ra0, dec0)).T
    bins = [np.arange(-nx - 300, nx + 300, 5), np.arange(-ny - 300, ny + 300, 5)]
    best = (0,)
    # coarse search: for each scale, parity and rotation, the offset most star-catalog pairs agree on
    for scale in (5.15, 5.21, 5.27):
        for parity in (1, -1):
            for angle in np.arange(0, 360, 1.0):
                guess = ((sky / scale) * [parity, 1]) @ _rotation(angle).T
                offsets = (stars[:, None, :2] - guess[None, :, :]).reshape(-1, 2)
                votes, xe, ye = np.histogram2d(offsets[:, 0], offsets[:, 1], bins=bins)
                i, j = np.unravel_index(votes.argmax(), votes.shape)
                if votes[i, j] > best[0]:
                    best = (votes[i, j], scale, parity, angle, xe[i] + 2.5, ye[j] + 2.5)
    _, scale, parity, angle, dx, dy = best
    guess = ((sky / scale) * [parity, 1]) @ _rotation(angle).T + [dx, dy]
    pairs = _matches(stars, guess[:, 0], guess[:, 1], 8)
    if len(pairs) < 6:
        return None
    # refine: a linear fit on the matched pairs, re-matched against the whole catalog with a tightening tolerance
    everything = np.array(tangent([c['ra'] for c in catalog], [c['dec'] for c in catalog], ra0, dec0)).T
    everything = np.c_[everything, np.ones(len(everything))]
    design = np.c_[sky[[j for _, j in pairs]], np.ones(len(pairs))]
    for tolerance in (5, 3, 2.5, None):
        px, py = stars[[i for i, _ in pairs], 0], stars[[i for i, _ in pairs], 1]
        cx, cy = np.linalg.lstsq(design, px, rcond=None)[0], np.linalg.lstsq(design, py, rcond=None)[0]
        if tolerance is None:
            break
        pairs = _matches(stars, everything @ cx, everything @ cy, tolerance)
        if len(pairs) < 6:
            return None
        design = everything[[j for _, j in pairs]]
    residual = np.hypot(design @ cx - px, design @ cy - py)

    def topix(ra, dec):
        x, y = tangent(ra, dec, ra0, dec0)
        return float(cx @ [x, y, 1]), float(cy @ [x, y, 1])
    return dict(topix=topix, nmatch=len(pairs), rms=float(np.sqrt((residual ** 2).mean())))
