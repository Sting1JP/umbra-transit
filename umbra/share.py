"""How well a night's curve shows the transit to someone who just looks at it. This sits beside the verdict and
changes nothing: a night can be good to upload and still be a poor picture."""
import re

from .grade import GOOD

CLEAR, SHOWS = 2.8, 1.5         # the score from which a dip is plain to see, and from which it shows through the noise
BASELINE, GAP = 30, 10          # minutes: clear baseline needed on each side, and the longest gap allowed in the transit


def rate(grade, report):
    """dict(dots, score, reason) for a night worth showing, dots from 1 to 3; None for a night that is not rated.

    Only a good night with nothing left to question is rated: EXOTIC passed it, the mid-time is within 2 sigma of
    the prediction, the depth within 30 % of the expected one, both baselines are there and the transit has no hole.
    """
    fit, transit, sky = report.get('fit') or {}, report.get('transit') or {}, report.get('sky') or {}
    if grade != GOOD or not fit.get('scatter') or not (fit.get('qc') or '').upper().startswith('PASS'):
        return None
    late = abs(fit['tc_bjd'] - transit['tc_bjd']) / max(fit['tc_err'], 1e-9)
    expected = fit.get('expected') or (transit.get('rp_rs') or 0) ** 2 or transit['depth']
    sides = min(sky.get('clear_pre_min', transit['pre_min']), sky.get('clear_post_min', transit['post_min']))
    if late > 2 or not 0.7 <= fit['depth'] / expected <= 1.3 or sides < BASELINE \
            or (report.get('gap') or {}).get('in_transit_min', 0) > GAP:
        return None
    # what the eye picks up: the dip against the noise between points. The rest only nudges it, each by 10 % at most.
    marks = re.search(r'([\d.]+) */ *5', fit['qc'])
    kept = (fit.get('frames') or 0) and 1 - (fit.get('dropped') or 0) / fit['frames']
    score = fit['depth'] / fit['scatter'] * (0.9 + 0.1 * kept) * (0.9 + 0.02 * float(marks.group(1)) if marks else 0.9) * (1 - 0.05 * late)
    if score >= CLEAR:
        return dict(dots=3, score=round(score, 2), reason='Clear dip, full baseline' if sides >= 45 else 'Clear dip')
    if score >= SHOWS:
        return dict(dots=2, score=round(score, 2), reason='Dip shows through some noise')
    return dict(dots=1, score=round(score, 2), reason='Dip hard to see by eye')
