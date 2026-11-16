"""The night log: every night with its verdict and reason, plus things worth telling the project."""
from collections import Counter
from datetime import datetime

from .directory import spaced
from .grade import missed

ORDER = ('good', 'improvable', 'lost')
_FOUND = dict(airmass='Runs that go above airmass 3', gap='Frames missing inside the transit',
              damaged='Frames with a failed readout', cut='Runs that cover only part of the transit',
              empty='Runs with no transit in them, by the ephemeris')


def _day(iso):
    d = datetime.fromisoformat(iso)
    return '%d %s' % (d.day, d.strftime('%b'))


def _name(night):
    return spaced(night['planet'] or night['target'])


def _frames(night):
    return (night.get('listing') or {}).get('frames') or []


def observations(nights):
    """Short factual notes drawn from all nights: sensor, scheduling and coverage problems."""
    notes = []
    # the directory's own "% clear" figure, night by night
    weather = [(n, {f.get('weather', -1) for f in _frames(n)}) for n in nights]
    weather = [(n, w) for n, w in weather if w and -1 not in w]
    run = []
    for n, w in reversed(weather):                      # newest first: how long has it been stuck?
        if len(w) == 1 and (not run or w == run[-1][1]):
            run.append((n, w))
        else:
            break
    if len(run) >= 3:
        notes.append('The directory\'s "%% clear" figure has read %d on every frame of the last %d nights (since %s). '
                     'MicroObservatory knows it can stick, a fault on their server. Umbra never uses the figure.'
                     % (next(iter(run[0][1])), len(run), _day(run[-1][0]['first_frame'])))
    wrong = []
    for n, w in weather:
        sky = ((n.get('report') or {}).get('sky') or {})
        frames = (n.get('report') or {}).get('frames') or 0
        if frames and min(w) >= 85 and (sky.get('overcast') or sky.get('clouded', 0) > 0.5 * frames):
            wrong.append('%s on %s' % (_name(n), _day(n['first_frame'])))
    if wrong:
        notes.append('The directory called these nights clear (85 or more) but the frames show cloud: %s.' % ', '.join(wrong))
    found = {key: [] for key in _FOUND}                 # one note for each kind of problem, naming its nights
    for n in nights:
        report, who = n.get('report') or {}, '%s on %s' % (_name(n), _day(n['first_frame']))
        transit, gap, sky = report.get('transit') or {}, report.get('gap') or {}, report.get('sky') or {}
        if transit.get('airmass') and transit['airmass'][2] > 3:
            found['airmass'].append('%s (%.1f)' % (who, transit['airmass'][2]))
        if gap.get('in_transit_min', 0) > 15 and not report.get('sampled'):
            found['gap'].append('%s (%d min)' % (who, round(gap['in_transit_min'])))
        if sky.get('damaged'):
            found['damaged'].append('%s (%d)' % (who, sky['damaged']))
        if transit and (transit['pre_min'] <= 0 or transit['post_min'] <= 0):
            found['empty' if missed(transit) else 'cut'].append(who)
    return notes + ['%s: %s.' % (_FOUND[key], ', '.join(who)) for key, who in found.items() if who]


def build(nights):
    """The log as plain data, for the screen."""
    graded = [n for n in nights if n['grade']]
    return dict(counts={g: sum(n['grade'] == g for n in graded) for g in ORDER}, waiting=len(nights) - len(graded),
                causes=Counter(n['headline'] for n in graded if n['grade'] == 'lost').most_common(),
                notes=observations(nights))


def markdown(nights, name):
    """The log as text, ready to paste or send."""
    log = build(nights)
    if not nights:
        return '# %s night log\n\nNo nights yet.\n' % name
    lines = ['# %s night log' % name, '',
             '%d nights from the MicroObservatory image directory, %s to %s.' % (
                 len(nights), _day(nights[0]['first_frame']), _day(nights[-1]['first_frame'])),
             ', '.join('%d %s' % (log['counts'][g], g) for g in ORDER) + ', %d waiting.' % log['waiting'], '']
    if log['causes']:
        lines += ['Lost to: ' + ', '.join('%s (%d)' % (c.lower(), k) for c, k in log['causes']) + '.', '']
    if log['notes']:
        lines += ['## Worth reporting', ''] + ['- ' + note for note in log['notes']] + ['']
    lines += ['## Nights', '', '| Date | Planet | Verdict | Reason | Detail |', '|---|---|---|---|---|']
    for n in nights:
        verdict = (n.get('report') or {}).get('verdict') or {}
        flagged = [c for c in verdict.get('checks', []) if c['state'] != 'ok']
        lines.append('| %s | %s | %s | %s | %s |' % (
            _day(n['first_frame']), _name(n), n['grade'] or 'waiting', n['headline'] or '',
            '; '.join('%s: %s' % (c['label'].lower(), c['value']) for c in flagged)))
    return '\n'.join(lines) + '\n'
