"""Reduce a night with EXOTIC, unattended, then check its answer against what Umbra measured itself.

EXOTIC is run as a separate program with no keyboard attached: if it asks a question it stops instead
of hanging. Clouded frames are left out, and every planetary parameter comes from ExoClock, so the run
does not depend on the NASA Exoplanet Archive being reachable.

Two EXOTICs are understood: the 4.3.1 release, and the 2026 line (4.3.2 development builds), which has a
switch for unattended runs, its own quality check, and a different layout of result files.
"""
import csv
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import SLUG
from .directory import spaced
from .grade import GOOD, IMPROVABLE

CREDIT = ('These observations were conducted with MicroObservatory, a robotic telescope network managed by the '
          'Harvard-Smithsonian Center for Astrophysics on behalf of NASA\'s Universe of Learning. This work is '
          'supported by NASA under award number NNX16AC65A to the Space Telescope Science Institute.')
MIN_FRAMES = 25
READ = 4            # raised when EXOTIC's files are read or checked differently, so stored fits are read again
NEWER_HELPS = ('No clear dip', 'Weak detection', 'Depth off', 'No comparison star')     # see verdict()
_NUM = r'([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)'
_FULL_PATH = re.compile(r'"(?:/|[A-Za-z]:\\)[^"]*[/\\]([^"/\\]+)"')    # a quoted absolute path, Unix or Windows; group 1 is the file name


def command():
    """How to start EXOTIC, or None when it is not installed next to this Python."""
    custom = os.environ.get(SLUG.upper() + '_EXOTIC')
    if custom:
        return shlex.split(custom)
    beside = Path(sys.executable).with_name('exotic')
    found = str(beside) if beside.exists() else shutil.which('exotic')
    return [found] if found else None


@functools.lru_cache(maxsize=None)
def _help(cmd):
    try:
        return subprocess.run(list(cmd) + ['--help'], capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def newer(cmd):
    """True when this EXOTIC is from the 2026 line. Asked of EXOTIC itself, once."""
    return '--non-interactive-run' in _help(tuple(cmd))


def ready(report):
    """True when a screening result holds what a reduction needs."""
    phot, series = report.get('phot') or {}, report.get('series') or {}
    return bool(phot.get('dx') and phot.get('comps') and series.get('file'))


def _left_out(count):
    return '%d clouded or damaged frame%s left out' % (count, '' if count == 1 else 's')


def prepare(folder, report, entry, observer, newer=False):
    """Lay out the working folder: the clear frames, the settings file, an empty output folder."""
    series, phot = report['series'], report['phot']
    keep = [i for i, cloudy in enumerate(series['cloudy']) if not cloudy]
    if len(keep) < MIN_FRAMES:
        raise ValueError('only %d clear frames' % len(keep))
    work = Path(folder) / 'reduce'
    if work.exists():
        shutil.rmtree(work)
    frames, out = work / 'frames', work / 'out'
    frames.mkdir(parents=True)
    out.mkdir()
    for i in keep:
        source = Path(folder) / series['file'][i]
        try:
            os.link(source, frames / source.name)           # no second copy on disk
        except OSError:
            shutil.copy2(source, frames / source.name)
    first = keep[0]                                         # EXOTIC places the stars from its first frame
    shift = lambda xy: [round(xy[0] + phot['dx'][first]), round(xy[1] + phot['dy'][first])]
    when = datetime.fromisoformat(series['t'][first].rstrip('Z'))
    dropped = len(series['cloudy']) - len(keep)
    darks = Path(folder) / 'darks'
    unc = lambda key, fallback: abs(entry.get(key) or 0) or fallback
    settings = {
        'user_info': {
            'Directory with FITS files': str(frames), 'Directory to Save Plots': str(out),
            'Directory of Flats': None, 'Directory of Biases': None,
            'Directory of Darks': str(darks) if any(darks.glob('*')) else None,
            'AAVSO Observer Code (N/A if none)': observer or 'N/A', 'Secondary Observer Codes (N/A if none)': 'MOBS',
            'Observation date': '%d-%s' % (when.day, when.strftime('%B-%Y')),
            'Obs. Latitude': '+31.68', 'Obs. Longitude': '-110.88', 'Obs. Elevation (meters)': 1268,
            'Camera Type (CCD or DSLR)': 'CCD', 'Pixel Binning': report.get('binning', '2x2'), 'Filter Name (aavso.org/filters)': 'CV',
            'Observing Notes': (_left_out(dropped) + '. ' if dropped else 'Clear sky. ') + CREDIT,
            'Plate Solution? (y/n)': 'n', 'Add Comparison Stars from AAVSO? (y/n)': 'n',
            'Target Star X & Y Pixel': shift([phot['x'], phot['y']]),
            'Comparison Star(s) X & Y Pixel': [shift(c) for c in phot['comps'][:5]],
            'Demosaic Format': None, 'Demosaic Output': None},
        'planetary_parameters': {
            'Target Star RA': entry['ra_j2000'], 'Target Star Dec': entry['dec_j2000'],
            'Planet Name': spaced(entry['name']), 'Host Star Name': entry['star'],
            'Orbital Period (days)': entry['ephem_period'], 'Orbital Period Uncertainty': unc('ephem_period_e1', 1e-6),
            # ExoClock gives BJD_TDB. The release only knows the old label (and reads it as TDB all the same).
            'Published Mid-Transit Time (%s)' % ('BJD_TDB' if newer else 'BJD-UTC'): entry['ephem_mid_time'],
            'Mid-Transit Time Uncertainty': unc('ephem_mid_time_e1', 1e-3),
            'Ratio of Planet to Stellar Radius (Rp/Rs)': entry['rp_over_rs'],
            'Ratio of Planet to Stellar Radius (Rp/Rs) Uncertainty': unc('rp_over_rs_e1', 0.005),
            'Ratio of Distance to Stellar Radius (a/Rs)': entry['sma_over_rs'],
            'Ratio of Distance to Stellar Radius (a/Rs) Uncertainty': unc('sma_over_rs_e1', 0.2),
            'Orbital Inclination (deg)': entry['inclination'], 'Orbital Inclination (deg) Uncertainty': unc('inclination_e1', 0.5),
            'Orbital Eccentricity (0 if null)': entry.get('eccentricity') or 0,
            'Argument of Periastron (deg)': entry.get('periastron') or 0,
            'Star Effective Temperature (K)': entry['teff'],
            'Star Effective Temperature (+) Uncertainty': unc('teff_e1', 100),
            'Star Effective Temperature (-) Uncertainty': -unc('teff_e2', 100),
            'Star Metallicity ([FE/H])': entry.get('meta') or 0,
            'Star Metallicity (+) Uncertainty': unc('meta_e1', 0.1), 'Star Metallicity (-) Uncertainty': -unc('meta_e2', 0.1),
            'Star Surface Gravity (log(g))': entry['logg'],
            'Star Surface Gravity (+) Uncertainty': unc('logg_e1', 0.1), 'Star Surface Gravity (-) Uncertainty': -unc('logg_e2', 0.1)},
        'optional_info': {'Pixel Scale (Ex: 5.21 arcsecs/pixel)': 5.21,
                          'Filter Minimum Wavelength (nm)': 350, 'Filter Maximum Wavelength (nm)': 850}}
    (work / 'inits.json').write_text(json.dumps(settings, indent=2))
    return work, dict(frames=len(keep), dropped=dropped)


def run(cmd, work, timeout, progress=None, stop=None, newer=False):
    """Run EXOTIC in work. Returns (finished, reason). progress(done, total) is called while frames are aligned."""
    log = work / 'exotic.log'
    env = dict(os.environ, MPLBACKEND='Agg', PYTHONUNBUFFERED='1')
    with open(log, 'wb') as sink:
        switches = ['-red', 'inits.json', '-ov'] + (['--non-interactive-run'] if newer else [])
        proc = subprocess.Popen(cmd + switches, cwd=work, stdin=subprocess.DEVNULL,
                                stdout=sink, stderr=subprocess.STDOUT, env=env)
        deadline = time.monotonic() + timeout                # this clock stops while the computer sleeps
        try:
            while proc.poll() is None:
                time.sleep(2)
                if time.monotonic() > deadline:
                    return False, 'EXOTIC took too long'
                if stop is not None and stop.is_set():
                    return False, 'stopped'
                if progress:
                    seen = re.findall(r'(?:Finding transformation|Pixel-aligning frame) (\d+) of (\d+)', log.read_text(errors='replace')[-4000:])
                    if seen:
                        progress(int(seen[-1][0]), int(seen[-1][1]))
        finally:                                            # never leave EXOTIC running behind us, even on Ctrl-C
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if proc.returncode:
        text = log.read_text(errors='replace')
        if 'EOFError' in text:
            return False, 'EXOTIC asked a question'
        last = [line.strip() for line in text.splitlines() if line.strip()]
        return False, 'EXOTIC stopped: ' + (last[-1][:120] if last else 'no output')
    return True, None


def _pair(text):
    found = re.findall(_NUM, text or '')
    return (float(found[0]), abs(float(found[1])) if len(found) > 1 else 0.0) if found else (None, None)


def collect(work, transit):
    """Read EXOTIC's result files into a plain dict, or None when they are missing or unreadable."""
    try:
        return _collect(Path(work), transit)
    except (KeyError, TypeError, ValueError, IndexError, OSError):
        return None


def _collect(work, transit):
    out = work / 'out'
    # the two EXOTICs use different subfolders; the newer one also keeps the fits of the comparison stars it
    # tried and rejected under Diagnostics, which are not the result
    find = lambda pattern: next((f for f in sorted(out.rglob(pattern)) if 'Diagnostics' not in f.parts), None)
    params, curve = find('FinalParams_*.json'), find('FinalLightCurve_*.csv')
    if not params or not curve:
        return None
    p = json.loads(params.read_text())['FINAL PLANETARY PARAMETERS']
    get = lambda *names: next((p[n] for n in names if p.get(n)), None)      # the newer name first, then the release's
    tc, tc_err = _pair(get('Mid-Transit Time (Tmid) best-fit with adjusted uncertainty', 'Mid-Transit Time (Tmid)'))
    area, area_err = _pair(get('Radius-ratio area depth (Rp/R*)^2', 'Transit depth (Rp/Rs)^2'))
    # The newer EXOTIC also fits a/Rs and the inclination. On a noisy night those trade against the radius, so
    # (Rp/R*)^2 can run far from the dip the data show. It reports that dip as well, with an error from the data:
    # that is the depth judged and shown. The release has only (Rp/Rs)^2.
    seen, expected = _pair(p.get('Observable model transit depth'))[0], _pair(p.get('Prior observable model transit depth'))[0]
    seen_err = _pair(p.get('Transit depth red-noise uncertainty'))[0]
    depth, depth_err = (seen, seen_err) if seen and seen_err else (area, area_err)
    rows = [[float(v) for v in row[:5]] for row in csv.reader(open(curve)) if row and not row[0].startswith('#')]
    if tc is None or depth is None or not rows:
        return None
    start = datetime.fromisoformat(transit['mid'].rstrip('Z'))             # BJD_TDB -> UTC through the predicted mid-time
    iso = lambda bjd: (start + timedelta(days=bjd - transit['tc_bjd'])).isoformat(timespec='seconds') + 'Z'
    comp = get('Transit Fit Comparison Star', 'Best Comparison Star') or 'None'
    log = work / 'exotic.log'
    version = re.search(r'Version ([^\s+]+)', log.read_text(errors='replace')) if log.exists() else None
    quality = ', '.join(v for v in (p.get('Transit detection QC'), p.get('KTMF') and 'KTMF ' + p['KTMF']) if v)
    report, plot = find('AAVSO_*.txt'), find('FinalLightCurve_*.png')
    return dict(read=READ, tc_bjd=tc, tc_err=tc_err, depth=depth / 100, depth_err=depth_err / 100,
                expected=expected / 100 if expected and depth is seen else None, area=area / 100, area_err=area_err / 100,
                rp_rs=_pair(get('Ratio of Planet to Stellar Radius (Rp/R*) best-fit with adjusted uncertainty',
                                'Ratio of Planet to Stellar Radius (Rp/R*)'))[0],
                scatter=_pair(get('Residual scatter around full model fit', 'Scatter in the residuals of the lightcurve fit is'))[0] / 100,
                duration_h=(_pair(p.get('Transit Duration (day)'))[0] or 0) * 24,
                comparison=None if comp.strip().lower().startswith('none') else comp, exotic=version.group(1) if version else None,
                qc=quality or None,                                          # EXOTIC's own verdict; the release has none
                curve=dict(t=[iso(r[0]) for r in rows], flux=[round(r[2], 4) for r in rows], model=[round(r[4], 5) for r in rows]),
                files=dict(report=str(report or ''), plot=str(plot or ''), folder=str(report.parent if report else out)))


def audit(path, observer, binning):
    """Is the AAVSO file ready to upload? Returns a check. The observer code and the binning reach the file
    through Umbra's settings, so when they are wrong there Umbra corrects the line itself. The newer EXOTIC
    also writes the full path of one of its files into the header; uploads are public, so the folder is
    taken out and only the file's name is left."""
    done = lambda state, value: dict(key='file', label='AAVSO file', state=state, value=value, why='AAVSO file not ready')
    path = Path(path) if path else None
    if not path or not path.is_file():
        return done('warn', 'not found')
    lines, fixed = path.read_text().splitlines(keepends=True), []
    for key, right in (('OBSCODE', observer), ('BINNING', binning)):
        at = next((i for i, text in enumerate(lines) if text.startswith('#%s=' % key)), None)
        if right and at is not None and lines[at].strip() != '#%s=%s' % (key, right):
            lines[at] = '#%s=%s\n' % (key, right)
            fixed.append('%s set to %s' % (key.lower(), right))
    bare = [_FULL_PATH.sub(r'"\1"', text) if text.startswith('#') else text for text in lines]
    if bare != lines:
        lines = bare
        fixed.append('local path removed')
    if any(str(Path.home()) in text for text in lines):     # none is expected to be left
        return done('warn', 'holds a path on this computer')
    if fixed:
        path.write_text(''.join(lines))
    head = dict(text[1:].strip().split('=', 1) for text in lines if text.startswith('#') and '=' in text)
    points = sum(1 for text in lines if text.strip() and not text.startswith('#'))
    if head.get('TYPE') != 'EXOPLANET' or points < MIN_FRAMES:
        return done('warn', 'incomplete, %d points' % points)
    if head.get('OBSCODE', 'N/A') in ('', 'N/A'):
        return done('warn', 'no observer code: start once with --observer CODE')
    return done('ok', '%s, %d points%s' % (head['OBSCODE'], points, '; ' + ', '.join(fixed) if fixed else ''))


def check(fit, transit, phot):
    """Does EXOTIC's answer hold up? Each check is judged against numbers EXOTIC did not produce."""
    checks = []
    add = lambda key, label, state, value, why=None: checks.append(dict(key=key, label=label, state=state, value=value, why=why))
    oc = (fit['tc_bjd'] - transit['tc_bjd']) * 1440
    allowed = max(15.0, 3 * fit['tc_err'] * 1440)
    add('timing', 'Timing', 'ok' if abs(oc) <= allowed else ('warn' if abs(oc) <= 3 * allowed else 'fail'),
        '%+.1f ± %.1f min from prediction' % (oc, fit['tc_err'] * 1440))
    expected = fit.get('expected') or (transit.get('rp_rs') or 0) ** 2 or transit['depth']
    ratio, strength = fit['depth'] / expected, fit['depth'] / max(fit['depth_err'], 1e-9)
    if strength < 2:
        state, why = 'fail', 'No clear dip'
    elif strength < 3:
        state, why = 'warn', 'Weak detection'
    else:
        state, why = ('ok', None) if 0.5 <= ratio <= 1.6 else ('warn', 'Depth off')
    add('depth', 'Depth', state, '%.2f ± %.2f %%, %.2f expected' % (fit['depth'] * 100, fit['depth_err'] * 100, expected * 100), why)
    if fit.get('duration_h') and transit.get('duration_h'):
        long = fit['duration_h'] / transit['duration_h']
        add('duration', 'Duration', 'ok' if 0.7 <= long <= 1.3 else 'warn',
            '%.1f h, %.1f expected' % (fit['duration_h'], transit['duration_h']), 'Duration off')
    # the dip in EXOTIC's own detrended points, without its model: in-transit mean against out-of-transit mean
    flux, model = fit['curve']['flux'], fit['curve']['model']
    floor = 1 - 0.5 * (1 - min(model))
    inside = [f for f, m in zip(flux, model) if m < floor]
    outside = [f for f, m in zip(flux, model) if m >= 1 - 1e-6]
    if len(inside) >= 5 and len(outside) >= 5:
        mean = lambda v: sum(v) / len(v)
        spread = lambda v: (sum((x - mean(v)) ** 2 for x in v) / (len(v) - 1)) ** 0.5
        dip = mean(outside) - mean(inside)
        err = (spread(inside) ** 2 / len(inside) + spread(outside) ** 2 / len(outside)) ** 0.5
        sigma = dip / err if err else 0
        add('dip', 'Dip in the points', 'ok' if sigma >= 3 else ('warn' if sigma >= 2 else 'fail'), '%.1f sigma' % sigma)
    else:
        add('dip', 'Dip in the points', 'warn', 'too few points')
    solid = all(c['state'] == 'ok' for c in checks if c['key'] in ('timing', 'depth', 'dip', 'duration'))
    if phot and phot.get('depth') is not None:
        gap = abs(fit['depth'] - phot['depth']) / max(1e-6, (fit['depth_err'] ** 2 + phot['depth_err'] ** 2) ** 0.5)
        # the quick look is the cruder of the two: it only counts against a fit that is already in doubt
        add('agree', 'Quick look', 'ok' if gap <= 3 or solid else 'warn', '%.1f %% there, %.1f sigma apart' % (phot['depth'] * 100, gap))
    add('comparison', 'Comparison star', 'ok' if fit['comparison'] or solid else 'warn', fit['comparison'] or 'none, target alone')
    if fit.get('qc'):
        passed = fit['qc'].upper().startswith('PASS')
        add('qc', "EXOTIC's own check", 'ok' if passed else ('warn' if fit['qc'].upper().startswith('MARGINAL') else 'fail'), fit['qc'])
    return checks


def verdict(checks, dropped, newer=False):
    """The night's final verdict once EXOTIC has run. newer: the fit came from the 2026 line."""
    failed = [c for c in checks if c['state'] == 'fail']
    warned = [c for c in checks if c['state'] == 'warn']
    if not failed and not warned:
        note = _left_out(dropped) if dropped else None
        return dict(grade=GOOD, headline='Transit recovered', remedy=note)
    worst = next((c for c in failed + warned if c['key'] != 'file'), warned[-1] if warned else failed[0])   # the file last
    headline = worst.get('why') or {'timing': 'Timing off', 'dip': 'No clear dip', 'agree': 'Fit and quick look disagree',
                                    'comparison': 'No comparison star', 'qc': 'EXOTIC doubts its fit'}[worst['key']]
    # The 2026 line picks comparison stars better and flattens the baseline before fitting, which has recovered
    # dips the 4.3.1 release lost. Suggest it when the release gave a weak answer.
    remedy = 'Try the newer EXOTIC' if headline in NEWER_HELPS and not newer else 'Check the fit before uploading'
    return dict(grade=IMPROVABLE, headline=headline, remedy=worst['value'] if worst['key'] == 'file' else remedy)
