"""The autopilot. Each night moves through fixed stages and can resume after a crash or sleep:

    new  --sample-->  sampled  --fetch-->  fetched  --screen-->  screened  --EXOTIC-->  done

A night is finished early when the listing shows a partial transit, when its sample frames are
clouded, or when screening calls it lost, so only promising nights are downloaded and reduced.
"""
import json
import re
import shutil
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import directory, ephemeris, grade, net, reduce, screen
from .config import KEEP_DAYS, RECENT_DAYS, SLUG, UPLOADS_URL
from .directory import Frame
from .store import DONE, FETCHED, NEW, SAMPLED, SCREENED

_ID = re.compile('^%s$' % directory.ID_PATTERN)
_MARKER = '.' + SLUG            # only folders holding this file are ever deleted
_SETTLE = timedelta(hours=6)    # a night is left alone until its last frame is this old
_CUT = timedelta(hours=3)       # a night starting this close to the start of a short listing may be incomplete
_NO_PLANET = 'No ephemeris for this planet'
_SLACK = timedelta(minutes=30)  # AAVSO dates an upload by its first point, in a slightly different time scale


def _utc(text):
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _pack(frames):
    return [dict(asdict(f), time=f.time.isoformat()) for f in frames]


def _unpack(rows):
    known = Frame.__dataclass_fields__
    return [Frame(**{k: datetime.fromisoformat(v) if k == 'time' else v for k, v in r.items() if k in known}) for r in rows]


class Pipeline:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.activity = None                # what the autopilot is doing right now, for the screen
        self.hold = self._held = None       # why it is waiting: 'offline', 'busy' or 'disk'
        self.refreshed = store.when('directory')        # a restart does not read the directory sooner than it was due
        self.asked_aavso = 0.0
        self._wake, self._stop = threading.Event(), threading.Event()
        self._full = 0
        settings.nights_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ discovery
    def due(self):
        """Whether the directory listing may be read again."""
        return time.time() - self.refreshed > self.settings.refresh_hours * 3600

    def refresh(self):
        """Read the directory and record every transit run in it: a long series of frames on one target."""
        self.activity = dict(stage='directory')
        # the full 30 days the first time or after a long break; otherwise the last 10 hold everything new
        recent = time.time() - self.store.when('directory') < (RECENT_DAYS - 1) * 86400
        frames = directory.listing(RECENT_DAYS if recent else KEEP_DAYS)
        if not frames:
            self.store.log(None, 'the directory listed no frames')
        nights = directory.name_nights(directory.group_nights(frames))
        darks = [f for f in frames if directory.is_dark(f)]
        cut = min(f.time for f in frames) + _CUT if recent and frames else None
        table, archive = ephemeris.load(self.settings.data / 'exoclock.json'), None
        now = datetime.now(timezone.utc)
        for nid, night in nights.items():
            if len(night) < self.settings.min_run or (cut and night[0].time < cut):     # the short listing may cut a night off
                continue
            known = self.store.get(nid, full=False)
            unknown = known and known['stage'] == DONE and not known['planet']     # looked up again: the tables grow
            if (known and known['stage'] != NEW and not unknown) or now - night[-1].time < _SETTLE:
                continue
            entry = ephemeris.lookup(night[0].target, table)
            if not entry:                                   # the archive is only read when ExoClock lacks a planet
                archive = self._archive() if archive is None else archive
                entry = ephemeris.from_archive(night[0].target, archive)
            fields = dict(target=night[0].target, first_frame=night[0].time.isoformat(), last_frame=night[-1].time.isoformat(),
                          frames=len(night), stage=NEW, grade=None, headline=None,
                          listing=dict(frames=_pack(night), darks=_pack(directory.pick_darks(night, darks)), entry=entry))
            mids = [(f.time + timedelta(seconds=f.exposure / 2)).replace(tzinfo=None) for f in night]
            try:
                transit = ephemeris.predict(mids, entry)[0] if entry else None
            except (KeyError, TypeError, ValueError):       # the record lacks a number the prediction needs
                entry = transit = None
            if transit is None:
                fields.update(stage=DONE, grade=grade.LOST, headline=_NO_PLANET)
            else:
                fields.update(planet=entry['name'], report=dict(transit=transit))
                if transit['pre_min'] <= 0 or transit['post_min'] <= 0:
                    verdict = grade.partial(transit)
                    fields.update(stage=DONE, grade=verdict['grade'], headline=verdict['headline'],
                                  report=dict(transit=transit, verdict=verdict))
            self.store.save(nid, **fields)
        self.store.mark('directory')
        self.refreshed = time.time()
        self.activity = None

    def _archive(self):
        try:
            return ephemeris.load_archive(self.settings.data / 'archive.json')
        except (IOError, ValueError) as exc:                # not worth holding the directory up for
            self.store.log(None, 'the NASA Exoplanet Archive could not be read: %s' % exc)
            return []

    # ------------------------------------------------------------------ stages
    def folder(self, nid):
        if not _ID.match(nid):
            raise ValueError('bad night id: %r' % nid)
        path = self.settings.nights_dir / nid
        (path / 'darks').mkdir(parents=True, exist_ok=True)
        (path / _MARKER).touch()
        return path

    def _download(self, nid, stage, frames, folder):
        for i, frame in enumerate(frames):
            if self._stop.is_set():
                raise InterruptedError
            self.activity = dict(night=nid, stage=stage, done=i, total=len(frames))
            if directory.download(frame, str(folder)):
                time.sleep(self.settings.fetch_pause)

    def _finish(self, nid, report, verdict, said=None):
        report['verdict'] = verdict
        self.store.save(nid, stage=DONE, grade=verdict['grade'], headline=verdict['headline'], report=report)
        self.store.log(nid, said or '%s: %s' % (verdict['grade'], verdict['headline']))

    def step(self, night):
        """Advance one night by one stage."""
        nid, listing = night['id'], night['listing']
        frames, entry, folder = _unpack(listing['frames']), listing['entry'], self.folder(night['id'])
        on_disk = len(list(folder.glob('*.FITS.gz')))

        if night['stage'] == NEW and on_disk < len(frames):
            self._download(nid, 'sample', directory.pick_sample(frames, self.settings.sample_frames), folder)
            self.activity = dict(night=nid, stage='sample')
            report = screen.screen(str(folder), entry, self.settings.min_stars, deep=False)
            report['sampled'] = True
            verdict = grade.sample(report, self.settings.sample_pass)
            if verdict:
                self._finish(nid, report, verdict)
            else:
                self.store.save(nid, stage=SAMPLED, headline='Sample clear', report=report)
        elif night['stage'] in (NEW, SAMPLED):
            if self.settings.max_full and self._full >= self.settings.max_full and on_disk < len(frames):
                return False                                # this run's download budget is spent
            free = shutil.disk_usage(self.settings.data).free / 1e9
            if on_disk < len(frames) and free < self.settings.min_free_gb:
                self._pause('disk', '%.1f GB free' % free)
                return False
            self._full += on_disk < len(frames)
            self._download(nid, 'fetch', frames, folder)
            self._download(nid, 'fetch', _unpack(listing['darks']), folder / 'darks')
            self.store.save(nid, stage=FETCHED)
        elif night['stage'] == FETCHED:
            self.activity = dict(night=nid, stage='screen')
            report = screen.screen(str(folder), entry, self.settings.min_stars)
            verdict = grade.grade(report)
            if verdict['grade'] != grade.LOST and reduce.command() and reduce.ready(report):
                report['verdict'] = verdict
                self.store.save(nid, stage=SCREENED, grade=verdict['grade'], headline=verdict['headline'], report=report)
            else:
                self._finish(nid, report, verdict)
        elif night['stage'] == SCREENED:
            self._reduce(nid, night['report'], entry, folder)
        return True

    def _exotic(self, nid, report, entry, folder, cmd, newer, ensemble=False):
        """One EXOTIC run on a night. Returns (fit, reason): the fit read from its files, or why there is none."""
        stage = 'second' if ensemble else 'reduce'
        self.activity = dict(night=nid, stage=stage)
        def tick(done, total):                              # frames are counted while aligning; the fit has no counter
            self.activity = dict(night=nid, stage=stage, done=done, total=total) if done < total \
                else dict(night=nid, stage='second' if ensemble else 'fit')
        try:
            work, used = reduce.prepare(folder, report, entry, self.settings.observer, newer, ensemble)
            finished, reason = reduce.run(cmd, work, self.settings.reduce_timeout, tick, self._stop, newer)
            fit = reduce.collect(work, report['transit']) if finished else None
            reason = reason or (None if fit else 'EXOTIC wrote no result')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            fit, reason = None, 'EXOTIC could not be set up: %s' % exc
        if reason == 'stopped':
            raise InterruptedError
        return (dict(fit, **used) if fit else None), reason

    def _reduce(self, nid, report, entry, folder):
        """Run EXOTIC on a screened night and settle the verdict on its checked result."""
        cmd = reduce.command()
        if not cmd:                                         # EXOTIC was removed since the night was screened
            return self._finish(nid, report, report['verdict'])
        newer = reduce.newer(cmd)
        if report.pop('redo', None) == 'second' and report.get('fit') and newer:
            return self._second(nid, report, entry, folder, cmd)
        fit, reason = self._exotic(nid, report, entry, folder, cmd, newer)
        for key in ('other_fit', 'second'):                 # a new first fit: an earlier second try no longer applies
            report.pop(key, None)
        if not fit:
            own = [c for c in report['verdict']['checks'] if c['key'] in grade.KEYS]    # without the checks of an earlier fit
            failed = dict(key='exotic', label='EXOTIC', state='fail', value=reason)
            report['fit'] = None
            return self._finish(nid, report, dict(grade=grade.IMPROVABLE, headline='EXOTIC did not finish', remedy=reason,
                                                  checks=own + [failed]))
        final = self._settle(report, fit)
        self._finish(nid, report, final)                    # saved first, so a stop during the second try loses nothing
        if final['grade'] == grade.IMPROVABLE and newer:
            self._second(nid, report, entry, folder, cmd)

    def _second(self, nid, report, entry, folder, cmd):
        """EXOTIC once more on an improvable night, with its ensemble of comparison stars, on the same frames.
        It replaces the first fit only when EXOTIC passes it and every check of Umbra's passes, the pointing check
        included; otherwise the first fit and its verdict stand and the second try's files are removed."""
        first = report['fit']
        fit, reason = self._exotic(nid, report, entry, folder, cmd, True, ensemble=True)
        if not fit:
            report['other_fit'], report['second'] = None, dict(kept=False, value='ensemble did not finish: %s' % reason)
        final = self._settle(report, first, fit)
        kept = report['second']['kept']
        if not kept:
            shutil.rmtree(folder / reduce.SECOND, ignore_errors=True)
            if report.get('other_fit'):
                report['other_fit'] = dict(report['other_fit'], files={})
        self._finish(nid, report, final, '%s: %s, after a second try with an ensemble' % (final['grade'], final['headline'])
                     if kept else 'second try with an ensemble not used (%s)' % report['second']['value'].split(': ', 1)[-1])

    def _judge(self, report, fit, newer):
        """The verdict on one of EXOTIC's results, checked against the screening."""
        own = [c for c in report['verdict']['checks'] if c['key'] in grade.KEYS]    # without the checks of an earlier fit
        own = [c for c in own if c['key'] != 'pointing']
        moved = [c for c in [grade.pointing(report)] if c]          # judged afresh: older verdicts were made without it
        checks = reduce.check(fit, report['transit'], report.get('phot'))
        checks.append(reduce.audit(fit['files'].get('report'), self.settings.observer, report.get('binning', '2x2')))
        held = [c for c in moved if c['state'] != 'ok']             # the fit cannot mend a jump in the transit
        return dict(reduce.verdict(checks + held, fit.get('dropped', 0), newer), checks=own + moved + checks)

    def _settle(self, report, first, second=None):
        """The verdict on the first fit, or on the ensemble's second try when that is better. The second is used only
        when the first is not good, EXOTIC passes the second, and every check of Umbra's passes on it."""
        verdict = self._judge(report, first, bool(first.get('qc')))
        report['fit'] = first
        if second:
            other = self._judge(report, second, True)
            if verdict['grade'] != grade.GOOD and other['grade'] == grade.GOOD and (second.get('qc') or '').upper().startswith('PASS'):
                report['fit'], report['other_fit'], verdict = second, first, other
                report['second'] = dict(kept=True, value='ensemble')
            else:
                why = 'first fit good' if verdict['grade'] == grade.GOOD else other['headline'] if other['grade'] != grade.GOOD \
                    else "no check from EXOTIC"
                report['other_fit'], report['second'] = second, dict(kept=False, value='ensemble not used: ' + why)
        if report.get('second'):
            tried = report['second']
            verdict['checks'] = verdict['checks'] + [dict(key='second', label='Second try', state='ok' if tried['kept'] else 'warn',
                                                          value=tried['value'])]
        return verdict

    def _reread(self, night, report):
        """Read EXOTIC's files for a finished night again, after a change in how they are read. EXOTIC is not run."""
        old, folder = report['fit'], self.folder(night['id'])
        used = dict(frames=old.get('frames', 0), dropped=old.get('dropped', 0))
        read = lambda name: (lambda fit: dict(fit, **used) if fit else None)(reduce.collect(folder / name, report['transit']))
        first = read(reduce.FIRST)
        if not first:                                       # the files are gone: keep the fit, and do not ask again
            report['fit'] = dict(old, read=reduce.READ)
            return self.store.save(night['id'], report=report)
        second = read(reduce.SECOND) if (folder / reduce.SECOND).is_dir() else None
        if report.get('second', {}).get('kept') and not second:     # the second try's files are gone: back to the first
            report.pop('other_fit', None)
            report['second'] = dict(kept=False, value='ensemble files gone')
        self._finish(night['id'], report, self._settle(report, first, second))

    def requeue(self):
        """Send finished nights back a stage when something has changed since they were judged."""
        for night in self.store.all(full=True):
            report = night['report'] or {}
            if night['stage'] != DONE:
                continue
            stale = report.get('rules', 1) < screen.RULES           # judged by older, cruder rules
            if night['failures'] >= 3 and not night['grade']:       # gave up on it
                if stale:
                    self.store.save(night['id'], stage=NEW, failures=0, headline=None)
                continue
            if 'sky' not in report:
                continue
            if report.get('fit') and report['fit'].get('read', 1) < reduce.READ:
                self._reread(night, report)
                continue
            if report.get('sampled'):
                if night['grade'] == grade.LOST and stale and not grade.sample(report, self.settings.sample_pass):
                    self.store.save(night['id'], stage=SAMPLED, grade=None, headline='Sample clear', cleaned=0)
                continue
            if night['cleaned'] or not any((self.settings.nights_dir / night['id']).glob('*.FITS.gz')):
                continue
            if night['grade'] == grade.LOST:
                if stale:                                           # a second look
                    self.store.save(night['id'], stage=FETCHED)
            elif not reduce.command():
                continue
            elif night['grade'] and 'fit' not in report:            # EXOTIC was installed later
                if reduce.ready(report) or stale:
                    self.store.save(night['id'], stage=SCREENED if reduce.ready(report) else FETCHED)
            elif report.get('fit', 0) is None and night['failures'] < 3 and time.time() - night['updated'] > 86400:
                self.store.save(night['id'], stage=SCREENED, failures=night['failures'] + 1)    # EXOTIC failed: another try a day later
            elif night['grade'] == grade.IMPROVABLE and report.get('fit') and not report['fit'].get('qc') \
                    and reduce.newer(reduce.command()):
                self.store.save(night['id'], stage=SCREENED)        # weak on the 4.3.1 release, and a newer EXOTIC is here now
            elif night['grade'] == grade.IMPROVABLE and report.get('fit') and 'second' not in report \
                    and reduce.newer(reduce.command()):
                report['redo'] = 'second'                           # judged before there was a second try: it gets one now
                self.store.save(night['id'], stage=SCREENED, report=report)

    def _pause(self, reason, detail):
        if self.hold != reason:                             # said once, not at every retry
            self.store.log(None, 'waiting, %s: %s' % (reason, detail))
        self.hold = self._held = reason

    def _unreachable(self, exc):
        self._pause('busy' if isinstance(exc, net.Busy) else 'offline', exc)

    def _gave_up(self, night):
        """Verdict fields for a night that failed three times."""
        age = datetime.now(timezone.utc) - datetime.fromisoformat(night['first_frame'])
        if night['stage'] in (NEW, SAMPLED) and age > timedelta(days=KEEP_DAYS):
            return dict(grade=grade.LOST, headline='Left the directory before it was fetched')
        return dict(headline='Could not be processed', report=dict(night['report'] or {}, rules=screen.RULES))

    def run_once(self):
        """Work through every unfinished night, oldest first. Returns the number of stages completed."""
        steps, self._held, self._full = 0, None, 0
        self.requeue()
        for row in self.store.all():
            while not self._stop.is_set() and self._held not in ('offline', 'busy'):
                night = self.store.get(row['id'])
                if night['stage'] == DONE:
                    break
                try:
                    if not self.step(night):
                        break
                    steps += 1
                except InterruptedError:
                    break
                except net.Unreachable as exc:              # no fault of the night: wait and try again
                    self._unreachable(exc)
                except Exception as exc:                    # one bad night must not stop the others
                    failures = night['failures'] + 1
                    self.store.log(night['id'], 'error: %s' % exc)
                    if failures >= 3:
                        self.store.save(night['id'], stage=DONE, failures=failures, **self._gave_up(night))
                    else:
                        self.store.save(night['id'], failures=failures)
                    break
        self.activity, self.hold = None, self._held
        self.clean()
        return steps

    def clean(self):
        """Delete the raw frames of lost nights once they are older than reject_keep_days."""
        cutoff = time.time() - self.settings.reject_keep_days * 86400
        root = self.settings.nights_dir.resolve()
        for night in self.store.all():
            if night['stage'] != DONE or night['grade'] == grade.GOOD or night['grade'] == grade.IMPROVABLE:
                continue
            path = (root / night['id']).resolve()
            if night['cleaned'] or night['updated'] > cutoff or not _ID.match(night['id']):
                continue
            if path.parent == root and (path / _MARKER).exists():
                shutil.rmtree(path)
            self.store.save(night['id'], cleaned=1)

    # ------------------------------------------------------------------ background worker
    def run_forever(self):
        while not self._stop.is_set():
            for job in (self.refresh, self.run_once) if self.due() else (self.run_once,):
                try:
                    job()
                except net.Unreachable as exc:
                    self._unreachable(exc)
                except Exception as exc:
                    self.store.log(None, 'error: %s' % exc)
                self.activity = None
            self._wake.wait(self.nap())
            self._wake.clear()

    def nap(self):
        """Seconds until the next pass: the usual pause, or less when the listing falls due sooner.
        An overdue listing (its last read failed) waits the usual pause, so a failing server is not asked at once."""
        usual = 120 if self.hold == 'offline' else 600
        due_in = self.refreshed + self.settings.refresh_hours * 3600 - time.time()
        return min(usual, due_in + 1) if due_in > 0 else usual

    def find_uploads(self):
        """Mark the good nights that are already on the observer's public AAVSO page.

        Returns the newly found nights with their AAVSO numbers, or None when AAVSO could not be read.
        """
        waiting = [n for n in self.store.all() if n['grade'] == grade.GOOD and not n['uploaded']]
        if not waiting or not self.settings.observer or time.time() - self.asked_aavso < 10:
            return {}
        self.asked_aavso = time.time()
        since = min(n['first_frame'] for n in waiting)[:10]
        try:
            rows = json.loads(net.get(UPLOADS_URL % (quote(self.settings.observer), since), timeout=20))
            there = [(directory.slug(r['fields']['exoplanet_name']), _utc(r['fields']['obs_date']), _utc(r['fields']['added_date']),
                      int(r['pk'])) for r in rows]
        except (IOError, ValueError, KeyError, TypeError):
            return None
        found = {}
        for night in waiting:
            first, last = _utc(night['first_frame']) - _SLACK, _utc(night['last_frame']) + _SLACK
            match = next(((added, number) for planet, seen, added, number in there
                          if planet == directory.slug(night['planet'] or '') and first <= seen <= last), None)
            if match:
                self.store.save(night['id'], uploaded=match[0].isoformat(timespec='seconds'))
                found[night['id']] = match[1]
        return found

    def stop(self):
        self._stop.set()
        self._wake.set()
