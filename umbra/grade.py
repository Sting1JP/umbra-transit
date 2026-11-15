"""Turn a screening result into a verdict: good, improvable or lost, with the checks behind it.

Each check returns (state, problem, value, remedy); state is 'ok', 'warn' or 'fail'. The first failing
check names the night's problem; warnings name what would improve it. Wording is kept short on purpose.
"""

GOOD, IMPROVABLE, LOST = 'good', 'improvable', 'lost'


def _coverage(s):
    t = s['transit']
    pre, post = max(t['pre_min'], 0), max(t['post_min'], 0)
    value = '%d / %d min baseline' % (pre, post)
    if t['pre_min'] <= 0 or t['post_min'] <= 0:
        return 'fail', 'Partial transit', value, None
    if min(pre, post) < 30:
        return 'warn', 'Short baseline', value, None
    return 'ok', None, value, None


def _sky(s):
    k = s['sky']
    if k['overcast']:
        return 'fail', 'Overcast all night', 'no clear frames', None
    cloud = k['clouded'] - k.get('damaged', 0)              # a damaged frame is left out too, but is not cloud
    value = '%d of %d clouded' % (cloud, s['frames'])
    if k['in_transit'] and k['clouded_in_transit'] > 0.5 * k['in_transit']:
        return 'fail', 'Cloud through the transit', '%d of %d clouded in transit' % (k['clouded_in_transit'], k['in_transit']), None
    if max(k['clear_pre_min'], k['clear_post_min']) < 30:
        return 'fail', 'No clear baseline', value, None
    if cloud:
        return 'warn', 'Passing cloud', value, 'Drop the %d clouded frames' % cloud
    return 'ok', None, 'clear', None


def _first_frame(s):
    if s['sky']['first_clear']:
        return 'ok', None, 'clear', None
    return 'warn', 'First frame clouded', 'clouded', 'Start from the first clear frame'


def _gaps(s):
    lost, total = s['gap']['in_transit_min'], s['transit']['duration_h'] * 60
    if lost > 0.4 * total:
        return 'fail', 'Gap in the transit', '%d min missing' % round(lost), None
    if lost > 0.15 * total:
        return 'warn', 'Gap in the transit', '%d min missing' % round(lost), None
    return 'ok', None, 'none' if lost < 1 else '%d min' % round(lost), None


def _airmass(s):
    a = s['transit']['airmass']
    value = '%.2f to %.2f' % (a[0], a[1])
    if a[2] > 2.5:
        return 'warn', 'Low in the sky', value, 'Drop frames above airmass 2.5'
    return 'ok', None, value, None


def _signal(s):
    p, expected = s.get('phot'), s['transit']['depth']
    if not p:
        return None
    if p.get('error') == 'baseline':
        return 'fail', 'No clear baseline', 'no light curve', None
    if p.get('error'):                      # Umbra's own limit, not the night's: the frames are kept
        reason = {'plate': 'Field not recognized', 'comparisons': 'No comparison stars'}[p['error']]
        return 'warn', reason, 'no light curve', 'Reduce by hand'
    if p['saturated']:
        return 'fail', 'Star saturated', 'peak %d ADU' % p['peak'], None
    if 'depth' not in p:
        return 'fail', 'No frames in transit', 'no depth', None
    value = '%.1f %% deep, %.1f sigma' % (p['depth'] * 100, p['snr'])
    if p['scatter'] > 3 * abs(p['depth']):
        return 'fail', 'Too noisy', 'scatter %.1f %%' % (p['scatter'] * 100), None
    if p['snr'] >= 5 and 0.5 * expected < p['depth'] < 1.6 * expected:
        return 'ok', None, value, None
    return 'warn', 'Faint dip', value, None


CHECKS = (('coverage', 'Coverage', _coverage), ('sky', 'Sky', _sky), ('first', 'First frame', _first_frame),
          ('gaps', 'Gaps', _gaps), ('airmass', 'Airmass', _airmass), ('signal', 'Signal', _signal))
KEYS = tuple(key for key, _, _ in CHECKS)


def missed(transit):
    """True when the predicted transit falls wholly outside the run."""
    return min(transit['pre_min'], transit['post_min']) < -60 * transit['duration_h']


def partial(transit):
    """Verdict for a night whose frames, by the directory listing alone, miss part of the transit."""
    value = '%d / %d min baseline' % (max(transit['pre_min'], 0), max(transit['post_min'], 0))
    return dict(grade=LOST, headline='No transit in the frames' if missed(transit) else 'Partial transit', remedy=None,
                checks=[dict(key='coverage', label='Coverage', state='fail', value=value)])


def sample(s, min_share):
    """Verdict from the sample frames alone, or None if the night deserves a full download."""
    k = s['sky']
    if not k['overcast'] and 1 - k['clouded'] / s['frames'] >= min_share:
        return None
    state, _, value, _ = _coverage(s)
    sky = 'no clear frames' if k['overcast'] else '%d of %d sampled frames clouded' % (k['clouded'], s['frames'])
    return dict(grade=LOST, headline='Overcast all night' if k['overcast'] else 'Cloud through the night', remedy=None,
                checks=[dict(key='coverage', label='Coverage', state=state, value=value),
                        dict(key='sky', label='Sky', state='fail', value=sky)])


def grade(s):
    """Verdict for a fully screened night."""
    checks, problems, remedies = [], [], []
    for key, label, fn in CHECKS:
        r = fn(s)
        if r is None:
            continue
        state, problem, value, remedy = r
        checks.append(dict(key=key, label=label, state=state, value=value))
        if problem:
            problems.append((state, problem))
        if remedy:
            remedies.append(remedy)
    failed = [p for st, p in problems if st == 'fail']
    warned = [p for st, p in problems if st == 'warn']
    if failed:
        return dict(grade=LOST, headline=failed[0], checks=checks, remedy=None)
    if warned:
        return dict(grade=IMPROVABLE, headline=warned[0], checks=checks, remedy=remedies[0] if remedies else None)
    return dict(grade=GOOD, headline='Clean transit', checks=checks, remedy=None)
