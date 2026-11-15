"""Run with: python -m pytest tests"""
import json
import os
import sys
import threading
from datetime import datetime, timezone

import numpy as np
import pytest

from umbra import digest, directory, ephemeris, grade, net, reduce, screen
from umbra.config import Settings
from umbra.pipeline import Pipeline
from umbra.server import _local_host, act, open_form
from umbra.__main__ import claim
from umbra.store import DONE, FETCHED, NEW, SCREENED, Store

ROW = ('<tr><td><a href="?openImage=true&openImageName={n}"></a>{n}</td><td>12-Oct-2026 04:21:39</td><td>JS9</td>'
       '<td>FITS</td><td>Main</td><td>60.00</td><td>&nbsp;Clear</td><td>{t}</td><td>Cecilia</td><td>AZ</td>'
       '<td>moguest</td><td>181</td><td>info</td><td>100% Clear</td></tr>')


def result(**over):
    """A clean, fully screened night; tests override single fields."""
    s = dict(frames=80, transit=dict(pre_min=60, post_min=60, duration_h=2.0, depth=0.02, airmass=[1.1, 1.3, 1.3]),
             sky=dict(overcast=False, clouded=0, in_transit=30, clouded_in_transit=0, first_clear=True,
                      clear_pre_min=60, clear_post_min=60),
             gap=dict(longest_min=3, in_transit_min=0),
             phot=dict(saturated=False, peak=1000, depth=0.021, snr=7.0, scatter=0.012))
    for key, value in over.items():
        s[key] = dict(s.get(key, {}), **value) if isinstance(value, dict) else value
    return s


def test_listing_parser_reads_rows_and_skips_bad_names():
    html = ROW.format(n='WASP-10261012042139', t='WASP-10') + ROW.format(n='<script>261012042139', t='x')
    frames = directory.parse_listing(html)
    assert [f.name for f in frames] == ['WASP-10261012042139']
    assert frames[0].time == datetime(2026, 10, 12, 4, 21, 39, tzinfo=timezone.utc) and frames[0].weather == 100


def test_nights_split_on_long_gaps():
    mk = lambda ts, scope='Cecilia': directory.Frame(
        'WASP-10' + ts, datetime.strptime(ts, '%y%m%d%H%M%S').replace(tzinfo=timezone.utc), 'WASP-10', scope, 60.0)
    nights = directory.group_nights([mk('261012042139'), mk('261012042439'), mk('261109010134')])
    assert [len(n) for n in nights] == [2, 1] and directory.night_id(nights[0]) == 'wasp10-20261012'
    # the same day: a long break in one telescope's run is one night, a second telescope is another
    broken = directory.group_nights([mk('261012042139'), mk('261012091500'), mk('261012050000', 'Donald')])
    assert {k: len(v) for k, v in directory.name_nights(broken).items()} == {'wasp10-20261012': 2, 'wasp10donald-20261012': 1}


def test_grades():
    assert grade.grade(result())['grade'] == grade.GOOD
    cloudy = grade.grade(result(sky=dict(clouded=40, clouded_in_transit=20)))
    assert (cloudy['grade'], cloudy['headline']) == (grade.LOST, 'Cloud through the transit')
    passing = grade.grade(result(sky=dict(clouded=6, clouded_in_transit=2)))
    assert passing['grade'] == grade.IMPROVABLE and passing['remedy'] == 'Drop the 6 clouded frames'
    assert grade.grade(result(transit=dict(post_min=-20)))['headline'] == 'Partial transit'
    assert grade.partial(dict(pre_min=-1569, post_min=1652, duration_h=2.5))['headline'] == 'No transit in the frames'
    assert grade.grade(result(phot=dict(depth=0.008, snr=3.0)))['grade'] == grade.IMPROVABLE
    assert grade.grade(result(phot=dict(saturated=True)))['grade'] == grade.LOST


def test_a_jump_inside_the_transit_keeps_a_night_from_good():
    times = ['2026-10-16T0%d:%02d:00Z' % (6 + k // 20, 3 * (k % 20)) for k in range(80)]
    window = dict(ingress='2026-10-16T07:18:00Z', egress='2026-10-16T08:48:00Z')
    night = lambda dy: result(transit=window, series=dict(t=times), phot=dict(dx=[0] * 80, dy=dy))
    steady, early = night([k % 3 for k in range(80)]), night([90] * 10 + [0] * 70)      # jitter; a re-pointing before ingress
    assert [grade.pointing(n)['value'] for n in (steady, early)] == ['steady', '1 jump, outside the transit']
    assert grade.grade(steady)['grade'] == grade.grade(early)['grade'] == grade.GOOD
    moved = night([0] * 40 + [90] * 40)
    assert grade.pointing(moved) == dict(key='pointing', label='Pointing', state='warn', value='1 jump in the transit, up to 90 px')
    assert (grade.grade(moved)['grade'], grade.grade(moved)['headline']) == (grade.IMPROVABLE, grade.MOVED)
    assert grade.pointing(result()) is None                 # no shifts measured: no claim either way
    sound = reduce.check(fitted(), TRANSIT, None)           # a sound fit does not undo the jump
    assert reduce.verdict(sound + [grade.pointing(moved)], 0, newer=True) == dict(
        grade=grade.IMPROVABLE, headline=grade.MOVED, remedy='Check the fit before uploading')


def test_a_night_umbra_cannot_measure_is_kept_not_lost():
    for error, headline in (('plate', 'Field not recognized'), ('comparisons', 'No comparison stars')):
        verdict = grade.grade(result(phot=dict(error=error)))
        assert (verdict['grade'], verdict['headline'], verdict['remedy']) == (grade.IMPROVABLE, headline, 'Reduce by hand')
    assert grade.grade(result(phot=dict(error='baseline')))['grade'] == grade.LOST


def test_sample_gate():
    gate = Settings().sample_pass
    assert grade.sample(result(frames=8, sky=dict(clouded=3)), gate) is None       # WASP-52 b, 18 Oct 2026: usable in full
    assert grade.sample(result(frames=8, sky=dict(clouded=4)), gate)['grade'] == grade.LOST


def test_clouded_frames_do_not_disqualify_comparison_stars():
    flux = np.array([[900., 800., 700.], [910., 0., 705.], [5., np.nan, -3.], [905., 810., 702.]])     # frame 3 is clouded
    peaks = np.array([[2000., 1500., 3900.], [2010., 1500., 3900.], [4095., np.nan, 4095.], [2005., 1500., 3900.]])
    clear = np.array([True, True, False, True])
    assert list(screen.steady(flux, peaks, clear)) == [True, False, False]     # star 2 vanishes in clear sky, star 3 is saturated
    assert not screen.steady(flux, peaks, np.ones(4, bool)).any()              # the old rule lost the night


def test_a_damaged_frame_is_neither_the_reference_nor_cloud():
    stars = np.array([580, 2087, 560, 600, 20])            # the second frame is a failed readout, the last is clouded
    cloudy = np.array([False, False, False, False, True])
    assert screen.reference(stars, cloudy) in (0, 3)
    left_out = grade.grade(result(sky=dict(clouded=1, damaged=1)))
    assert left_out['grade'] == grade.GOOD


def test_blank_frames_do_not_stop_the_screening():
    blank = [np.zeros((60, 80)) for _ in range(4)]         # an overcast night: nothing to line up, not even the reference
    dx, dy, locked = screen._drift(blank, 0)
    assert not locked.any() and not dx.any() and not dy.any()


def test_one_wild_point_does_not_spoil_the_quick_look():
    t = np.linspace(-0.1, 0.1, 81)
    in_transit = abs(t) < 0.04
    rel = (1 + 0.3 * t) * np.where(in_transit, 0.98, 1.0) + 0.002 * np.sin(np.arange(81) * 2.4)
    rel[1] = 2.25                                           # a satellite crossed the star
    flat, use = screen.flatten(rel, t, ~in_transit, in_transit, np.ones(81, bool))
    assert not use[1] and use.sum() == 80
    assert abs(flat[~in_transit & use].mean() - flat[in_transit].mean() - 0.02) < 0.002 and flat[~in_transit & use].std() < 0.003


def test_night_log_spots_a_stuck_weather_figure():
    def night(day, weather, clouded):
        return dict(planet='WASP-10b', target='WASP-10', first_frame='2026-10-%02dT04:00:00+00:00' % day, grade='lost',
                    headline='Cloud through the night', listing=dict(frames=[dict(weather=w) for w in weather]),
                    report=dict(frames=8, sky=dict(clouded=clouded, overcast=False), verdict=dict(checks=[])))
    nights = [night(12, [100, 100], 6), night(22, [67, 67], 0), night(23, [67, 67], 0), night(24, [67, 67], 0)]
    notes = digest.observations(nights)
    assert any('has read 67 on every frame of the last 3 nights' in n for n in notes)
    assert any('called these nights clear' in n and '12 Oct' in n for n in notes)
    assert '| 12 Oct | WASP-10 b | lost |' in digest.markdown(nights, 'Umbra')


TRANSIT = dict(tc_bjd=2461329.8631, mid='2026-10-16T08:34:48Z', rp_rs=0.131, depth=0.0217, duration_h=2.58)


def fitted(depth=0.0172, depth_err=0.002, tc=2461329.8640, dip=0.017):
    model = [1.0] * 20 + [1 - dip] * 30 + [1.0] * 20
    flux = [m + (0.004 if i % 2 else -0.004) for i, m in enumerate(model)]
    return dict(tc_bjd=tc, tc_err=0.0015, depth=depth, depth_err=depth_err, scatter=0.012, comparison='#2 - [245, 87]', duration_h=2.5,
                curve=dict(t=[], flux=flux, model=model))


def test_fit_checks_accept_a_sound_result_and_catch_a_bad_one():
    good = reduce.check(fitted(), TRANSIT, dict(depth=0.016, depth_err=0.003))
    assert all(c['state'] == 'ok' for c in good)
    assert reduce.verdict(good, 12) == dict(grade=grade.GOOD, headline='Transit recovered', remedy='12 clouded or damaged frames left out')
    crude = reduce.check(fitted(), TRANSIT, dict(depth=0.035, depth_err=0.002))      # a sound fit outranks a crude quick look
    assert reduce.verdict(crude, 0)['grade'] == grade.GOOD
    late = reduce.check(fitted(tc=2461329.8631 + 60 / 1440), TRANSIT, None)
    assert next(c for c in late if c['key'] == 'timing')['state'] == 'fail'
    weak = reduce.check(fitted(depth=0.0153, depth_err=0.0063), TRANSIT, None)
    assert reduce.verdict(weak, 0)['headline'] == 'Weak detection'
    assert reduce.verdict(weak, 0)['remedy'] == 'Try the newer EXOTIC'
    assert reduce.verdict(weak, 0, newer=True)['remedy'] == 'Check the fit before uploading'
    doubted = reduce.check(dict(fitted(), qc='MARGINAL, KTMF 2.60 / 5.00'), TRANSIT, None)
    assert reduce.verdict(doubted, 0, newer=True)['headline'] == 'EXOTIC doubts its fit'
    assert reduce.verdict(late, 0)['remedy'] == 'Check the fit before uploading'
    flat = reduce.check(fitted(depth=0.003, depth_err=0.004, dip=0.0), TRANSIT, None)
    assert reduce.verdict(flat, 0)['headline'] == 'No clear dip' and reduce.verdict(flat, 0)['grade'] == grade.IMPROVABLE


def test_exotic_results_are_read_from_its_files(tmp_path):
    temp = tmp_path / 'out' / 'temp'
    temp.mkdir(parents=True)
    (temp / 'FinalParams_WASP-11 b_16-October-2026.json').write_text(json.dumps({'FINAL PLANETARY PARAMETERS': {
        'Mid-Transit Time (Tmid)': '2461329.8640 +/- 0.0015 BJD_TDB', 'Ratio of Planet to Stellar Radius (Rp/R*)': '0.131 +/- 0.008',
        'Transit depth (Rp/Rs)^2': '1.72 +/- 0.21 [%]', 'Scatter in the residuals of the lightcurve fit is': '1.2 %',
        'Best Comparison Star': 'None', 'Transit Duration (day)': '0.107 +/- 0.002'}}))
    (temp / 'FinalLightCurve_WASP-11 b_16-October-2026.csv').write_text(
        '# FINAL TIMESERIES OF WASP-11 b\n# BJD_TDB,Orbital Phase,Flux,Uncertainty,Model,Airmass\n2461329.8631, 0.0, 0.983, 0.01, 0.9828, 1.1\n')
    fit = reduce.collect(tmp_path, TRANSIT)
    assert fit['tc_bjd'] == 2461329.8640 and abs(fit['depth'] - 0.0172) < 1e-9 and fit['comparison'] is None
    assert fit['curve']['t'] == ['2026-10-16T08:34:48Z'] and abs(fit['duration_h'] - 2.568) < 1e-6


def test_the_newer_exotic_keeps_its_files_elsewhere(tmp_path):
    out = tmp_path / 'out'
    (out / 'AAVSO_Files').mkdir(parents=True)
    (out / 'FinalParams_WASP-50b_24-October-2026.json').write_text(json.dumps({'FINAL PLANETARY PARAMETERS': {
        'Transit detection QC': 'PASS', 'KTMF': '4.00 / 5.00', 'Mid-Transit Time (Tmid)': '2461337.8667 -0.0022/+0.0020 BJD_TDB',
        'Mid-Transit Time (Tmid) best-fit with adjusted uncertainty': '2461337.8674 +/- 0.0023 BJD_TDB',
        'Radius-ratio area depth (Rp/R*)^2': '1.57 +/- 0.41 [%]', 'Residual scatter around full model fit': '1.0263 %',
        'Transit Fit Comparison Star': '#2 - [523.0, 129.0]', 'Transit Duration (day)': '0.0743 +/- 0.0085'}}))
    (out / 'AAVSO_Files' / 'FinalLightCurve_WASP-50b_24-October-2026.csv').write_text(
        '# BJD_TDB,Orbital Phase,Flux,Uncertainty,Model,Airmass,Detrend,Mag,Unc,Mag,Unc,Apparent,Unc,Band\n'
        '2461337.79, -0.038, 0.990, 0.011, 1.0, 1.45, 1.007, -0.19, 0.009, -0.19, 0.009, na, na, CV\n')
    (out / 'AAVSO_Files' / 'AAVSO_WASP-50b_24-October-2026.txt').write_text('#TYPE=EXOPLANET\n')
    fit = reduce.collect(tmp_path, TRANSIT)
    assert (fit['tc_bjd'], fit['tc_err'], fit['qc']) == (2461337.8674, 0.0023, 'PASS, KTMF 4.00 / 5.00')
    assert abs(fit['depth'] - 0.0157) < 1e-9 and fit['comparison'] == '#2 - [523.0, 129.0]'
    assert fit['files']['folder'].endswith('AAVSO_Files') and fit['curve']['flux'] == [0.99]


def test_the_result_is_the_final_fit_and_its_depth_is_the_dip_seen(tmp_path):
    def params(folder, **values):
        folder.mkdir(parents=True)
        (folder / 'FinalParams_WASP-52b_8-November-2026.json').write_text(json.dumps({'FINAL PLANETARY PARAMETERS': dict({
            'Mid-Transit Time (Tmid)': '2461352.7292 -0.0016/+0.0016 BJD_TDB', 'Residual scatter around full model fit': '1.0372 %',
            'Transit Fit Comparison Star': '#5 - [301.0, 212.0]'}, **values)}))
    out = tmp_path / 'out'
    # a comparison star EXOTIC tried and dropped: its fit sorts first and is not the result
    params(out / 'Diagnostics' / 'comp1' / 'working_artifacts', **{'Transit detection QC': 'FAIL', 'KTMF': '2.10 / 5.00',
                                                                   'Radius-ratio area depth (Rp/R*)^2': '0.4 +/- 0.9 [%]'})
    params(out / 'working_artifacts', **{'Transit detection QC': 'MARGINAL', 'KTMF': '3.61 / 5.00',
                                         'Radius-ratio area depth (Rp/R*)^2': '11.8 +/- 4.0 [%]', 'Observable model transit depth': '3.4 +/- 3.7 [%]',
                                         'Prior observable model transit depth': '2.987 +/- 0.062 [%]', 'Transit depth red-noise uncertainty': '+/- 0.2820 %'})
    (out / 'AAVSO_Files').mkdir()
    (out / 'AAVSO_Files' / 'FinalLightCurve_WASP-52b_8-November-2026.csv').write_text('2461352.73, 0.0, 0.966, 0.01, 0.966, 1.2\n')
    fit = reduce.collect(tmp_path, TRANSIT)
    assert fit['qc'] == 'MARGINAL, KTMF 3.61 / 5.00' and fit['read'] == reduce.READ
    assert (round(fit['depth'], 4), round(fit['depth_err'], 5), round(fit['expected'], 5), round(fit['area'], 3)) == (0.034, 0.00282, 0.02987, 0.118)


def test_a_stored_fit_is_read_again_without_running_exotic(tmp_path, monkeypatch):
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    out = pipe.folder('wasp50-20261024') / 'reduce' / 'out'
    out.mkdir(parents=True)
    (pipe.folder('wasp50-20261024') / 'frame.FITS.gz').write_bytes(b'x')
    (out / 'FinalParams_WASP-50b.json').write_text(json.dumps({'FINAL PLANETARY PARAMETERS': {
        'Transit detection QC': 'PASS', 'KTMF': '4.20 / 5.00', 'Mid-Transit Time (Tmid)': '2461329.8640 +/- 0.0015 BJD_TDB',
        'Radius-ratio area depth (Rp/R*)^2': '1.51 +/- 0.50 [%]', 'Observable model transit depth': '1.86 +/- 0.53 [%]',
        'Prior observable model transit depth': '2.060 +/- 0.065 [%]', 'Transit depth red-noise uncertainty': '+/- 0.2572 %',
        'Residual scatter around full model fit': '1.0049 %', 'Transit Fit Comparison Star': '#2 - [523.0, 129.0]'}}))
    (out / 'FinalLightCurve_WASP-50b.csv').write_text(''.join(
        '%.4f, 0, %.4f, 0.01, %.4f, 1.1\n' % (2461329.80 + k / 400, flux + 0.002 * (-1) ** k, flux)
        for k, flux in enumerate([1.0] * 12 + [0.981] * 16 + [1.0] * 12)))
    (out / 'AAVSO_WASP-50b.txt').write_text('#TYPE=EXOPLANET\n#OBSCODE=SSDB\n#BINNING=2x2\n' + '2461329.8,0.99,0.01,1.1,1.0\n' * 40)
    stored = dict(result(transit=TRANSIT, phot=dict(depth_err=0.003)), verdict=dict(grade=grade.IMPROVABLE, headline='No clear dip', checks=[]),
                  fit=dict(depth=0.004, depth_err=0.006, qc='FAIL, KTMF 2.10 / 5.00', frames=40, dropped=3))     # read from the wrong file
    store.save('wasp50-20261024', stage=DONE, grade=grade.IMPROVABLE, first_frame='2026-10-24T06:45:36+00:00', report=stored)
    monkeypatch.setattr(reduce, 'command', lambda: None)        # EXOTIC is not needed for this
    pipe.requeue()
    night = store.get('wasp50-20261024')
    assert (night['grade'], night['headline']) == (grade.GOOD, 'Transit recovered')
    assert night['report']['fit']['read'] == reduce.READ and night['report']['fit']['dropped'] == 3


def test_the_aavso_file_is_checked_and_its_own_lines_put_right(tmp_path):
    file = tmp_path / 'out' / 'AAVSO_Files' / 'AAVSO_WASP-11b.txt'
    file.parent.mkdir(parents=True)
    rows = ''.join('2461329.%04d,0.99,0.01,1.1,1.0\n' % k for k in range(40))
    timing = '#TIMING-XC={"report_path": "%s", "frame_count": 40}\n' % (tmp_path / 'out' / 'TimestampSelection.json')
    file.write_text('#TYPE=EXOPLANET\n#OBSCODE=N/A\n#SECONDARY_OBSCODES=MOBS\n#BINNING=1x1\n' + timing + '#DATE,FLUX,MERR,DETREND_1,DETREND_2\n' + rows)
    assert reduce.audit(tmp_path / 'missing.txt', 'SSDB', '2x2')['state'] == 'warn'
    assert reduce.audit(file, '', '2x2')['value'] == 'no observer code: start once with --observer CODE'
    assert '"report_path": "TimestampSelection.json"' in file.read_text() and str(tmp_path) not in file.read_text()
    fixed = reduce.audit(file, 'SSDB', '2x2')
    assert fixed['state'] == 'ok' and 'obscode set to SSDB' in fixed['value']
    assert '#OBSCODE=SSDB\n' in file.read_text() and '#BINNING=2x2\n' in file.read_text() and file.read_text().endswith(rows)
    assert reduce.audit(file, 'SSDB', '2x2')['value'] == 'SSDB, 40 points'
    only = reduce.verdict([dict(key='file', label='AAVSO file', state='warn', value='not found', why='AAVSO file not ready')], 0)
    assert (only['headline'], only['remedy']) == ('AAVSO file not ready', 'not found')


def test_a_planet_exoclock_lacks_comes_from_the_archive():
    row = dict(pl_name='TOI-6842 A b', hostname='TOI-6842', ra=20.6369352, dec=13.3522271, pl_orbper=5.3917436, pl_orbpererr1=2.5e-06,
               pl_tranmid=2460209.33933, pl_tranmiderr1=0.00028, pl_trandur=4.3656, pl_trandep=1.634, pl_ratror=0.12147, pl_ratdor=None,
               pl_orbsmax=0.0625, st_rad=1.445, pl_orbincl=86.67, pl_orbeccen=0.0, pl_orblper=90.0, st_teff=5725.0, st_tefferr1=76.0,
               st_tefferr2=-72.0, st_met=-0.02, st_meterr1=0.096, st_meterr2=-0.085, st_logg=4.152, st_loggerr1=0.02, st_loggerr2=-0.019,
               sy_vmag=12.165)
    entry = ephemeris.from_archive('TOI6842', [row])
    assert (entry['name'], entry['source'], entry['ra_j2000'], entry['dec_j2000']) == ('TOI-6842Ab', 'NASA Exoplanet Archive', '01:22:32.86', '+13:21:08.0')
    assert abs(entry['sma_over_rs'] - 9.3) < 0.01 and abs(1 - 10 ** (-entry['depth_r_mmag'] / 2500) - 0.01634) < 1e-9
    assert directory.spaced(entry['name']) == 'TOI-6842 A b'
    assert ephemeris.from_archive('TOI1455', [row]) is None and ephemeris.from_archive('TOI6842', [dict(row, st_logg=None)]) is None


def test_uploads_are_remembered_and_only_umbras_own_files_are_shown(tmp_path, monkeypatch):
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    own = pipe.folder('wasp50-20261024') / 'reduce' / 'out' / 'AAVSO_WASP-50b.txt'
    own.parent.mkdir(parents=True)
    own.write_text('#TYPE=EXOPLANET\n')
    plot = own.with_name('FinalLightCurve_WASP-50b.png')
    plot.write_bytes(b'png')
    own.with_name('Triangle_WASP-50b.png').write_bytes(b'not for upload')
    other = tmp_path / 'elsewhere.txt'
    other.write_text('not a report')
    store.save('wasp50-20261024', stage=DONE, grade=grade.GOOD, first_frame='2026-10-24T06:45:36+00:00',
               report=dict(fit=dict(files=dict(report=str(own), plot=str(plot)))))
    store.save('wasp50-20261028', stage=DONE, grade=grade.GOOD, first_frame='2026-10-28T06:45:36+00:00',
               report=dict(fit=dict(files=dict(report=str(other)))))
    shown = []
    monkeypatch.setattr('umbra.server.subprocess.Popen', lambda cmd, **kw: shown.append(cmd))
    upload = pipe.folder('wasp50-20261024') / 'upload'
    assert act(pipe, 'reveal', 'wasp50-20261024') and act(pipe, 'reveal', 'wasp50-20261024') and shown.pop() == shown[0]
    assert shown[0][-1] == str(upload) and sorted(path.name for path in upload.iterdir()) == [own.name, plot.name]
    assert not act(pipe, 'reveal', 'wasp50-20261028') and not act(pipe, 'reveal', 'wasp50-20261101') and len(shown) == 1
    assert act(pipe, 'uploaded', 'wasp50-20261024') and store.get('wasp50-20261024')['uploaded'].startswith('20')
    pipe._finish('wasp50-20261024', dict(), dict(grade=grade.GOOD, headline='Transit recovered'))     # judged again later
    assert store.get('wasp50-20261024')['uploaded'] and Store(settings.db_path).get('wasp50-20261024')['uploaded']
    assert act(pipe, 'pending', 'wasp50-20261024') and store.get('wasp50-20261024')['uploaded'] is None
    assert not act(pipe, 'uploaded', 'wasp50-20261101')
    opened = []
    monkeypatch.setattr('umbra.server.webbrowser.open', lambda url: opened.append(url) or True)
    assert open_form() and opened == ['https://apps.aavso.org/exosite/submit']       # the form itself, never a sign-in address


def test_nights_already_on_aavso_are_found(tmp_path, monkeypatch):
    settings = Settings(data=tmp_path, observer='SSDB')
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    for nid, planet, first, last in (('gaia1-20261015', 'Gaia-1b', '2026-10-15T07:26:21', '2026-10-15T11:42:18'),
                                     ('gaia1-20261018', 'Gaia-1b', '2026-10-18T08:39:53', '2026-10-18T13:20:02'),
                                     ('wasp50-20261024', 'WASP-50b', '2026-10-24T06:45:36', '2026-10-24T10:36:21')):
        store.save(nid, stage=DONE, grade=grade.GOOD, planet=planet, first_frame=first + '+00:00', last_frame=last + '+00:00')
    row = lambda planet, seen: dict(pk=19702, fields=dict(exoplanet_name=planet, obs_date=seen, added_date='2026-11-15T04:35:12'))
    asked = []
    listing = [row('Gaia-1 b', '2026-10-15T07:34:36'), row('WASP-50 b', '2026-10-28T04:40:00'), row('WASP-12 b', '2026-10-24T07:00:00')]
    monkeypatch.setattr(net, 'get', lambda url, timeout=0: asked.append(url) or json.dumps(listing).encode())
    assert pipe.find_uploads() == {'gaia1-20261015': 19702} and 'obscode=SSDB' in asked[0] and asked[0].endswith('start_date=2026-10-15')
    assert store.get('gaia1-20261015')['uploaded'] == '2026-11-15T04:35:12+00:00'
    assert not store.get('gaia1-20261018')['uploaded'] and not store.get('wasp50-20261024')['uploaded']    # another night, another planet
    assert pipe.find_uploads() == {} and len(asked) == 1                                   # not asked again straight away
    pipe.asked_aavso = 0
    monkeypatch.setattr(net, 'get', lambda url, timeout=0: (_ for _ in ()).throw(net.Unreachable('down')))
    assert pipe.find_uploads() is None and not store.get('wasp50-20261024')['uploaded']
    assert Pipeline(Settings(data=tmp_path, observer=''), store).find_uploads() == {}      # no observer code: nothing to look up


def test_exotic_never_gets_a_keyboard(tmp_path):
    (tmp_path / 'inits.json').write_text('{}')
    asks = [sys.executable, '-c', 'input("Enter 1 or 2: ")']
    assert reduce.run(asks, tmp_path, 30) == (False, 'EXOTIC asked a question')
    hangs = [sys.executable, '-c', 'import time; time.sleep(60)']
    assert reduce.run(hangs, tmp_path, 1) == (False, 'EXOTIC took too long')


def test_stopping_takes_exotic_down_too(tmp_path):
    stop = threading.Event()
    stop.set()
    sleeper = [sys.executable, '-c', 'import os, time; open("pid", "w").write(str(os.getpid())); time.sleep(60)']
    assert reduce.run(sleeper, tmp_path, 30, stop=stop) == (False, 'stopped')
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'pid').read_text()), 0)


def test_network_is_allow_listed():
    for url in ('https://example.com/x', 'http://mo-www.cfa.harvard.edu/ImageDirectory/x.FITS', 'file:///etc/passwd'):
        with pytest.raises(ValueError):
            net.get(url)


def test_download_rejects_odd_names(tmp_path):
    frame = directory.Frame('../../etc/passwd', datetime.now(timezone.utc), 'x', 'Cecilia', 60.0, 1)
    with pytest.raises(ValueError):
        directory.download(frame, str(tmp_path))


def test_host_header_check():
    assert _local_host('localhost:8765', 8765) and _local_host('127.0.0.1:8765', 8765) and _local_host('192.168.1.20:8765', 8765)
    assert not _local_host('evil.example:8765', 8765) and not _local_host('localhost:9999', 8765) and not _local_host(None, 8765)


def test_cleanup_only_removes_marked_night_folders(tmp_path):
    settings = Settings(data=tmp_path, reject_keep_days=0)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    lost = pipe.folder('wasp10-20261012')
    (lost / 'frame.FITS.gz').write_bytes(b'x')
    unmarked = settings.nights_dir / 'qatar1-20261014'
    unmarked.mkdir()
    keep = pipe.folder('hatp10-20261016')
    for nid, g in (('wasp10-20261012', grade.LOST), ('qatar1-20261014', grade.LOST), ('hatp10-20261016', grade.GOOD)):
        store.save(nid, stage=DONE, grade=g, first_frame='2026-10-12T04:21:39+00:00')
    pipe.clean()
    assert not lost.exists() and unmarked.exists() and keep.exists()
    with pytest.raises(ValueError):
        pipe.folder('../outside')


def test_a_rule_change_gives_lost_nights_a_second_look(tmp_path):
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    for nid, report in (('wasp50-20261026', dict(sky={})), ('wasp50-20261028', dict(sky={}, rules=screen.RULES))):
        (pipe.folder(nid) / 'frame.FITS.gz').write_bytes(b'x')
        store.save(nid, stage=DONE, grade=grade.LOST, first_frame='2026-10-26T05:39:28+00:00', report=report)
    pipe.requeue()
    assert store.get('wasp50-20261026')['stage'] == FETCHED and store.get('wasp50-20261028')['stage'] == DONE


def test_a_night_is_not_screened_again_for_nothing(tmp_path, monkeypatch):
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    monkeypatch.setattr(reduce, 'command', lambda: ['exotic'])
    current = dict(sky={}, rules=screen.RULES, phot=dict(error='comparisons'))      # nothing for EXOTIC to start from
    for nid, report in (('wasp52-20261018', current), ('wasp52-20261108', dict(current, rules=screen.RULES - 1))):
        (pipe.folder(nid) / 'frame.FITS.gz').write_bytes(b'x')
        store.save(nid, stage=DONE, grade=grade.IMPROVABLE, first_frame='2026-10-18T03:42:36+00:00', report=report)
    # a night given up on after three errors starts over once the rules have changed, and only then
    store.save('hatp32-20261105', stage=DONE, failures=3, headline='Could not be processed',
               first_frame='2026-11-05T07:29:10+00:00', report=dict(rules=screen.RULES - 1))
    store.save('hatp32-20261103', stage=DONE, failures=3, headline='Could not be processed',
               first_frame='2026-11-03T07:29:10+00:00', report=dict(rules=screen.RULES))
    pipe.requeue()
    stage = lambda nid: store.get(nid)['stage']
    assert stage('wasp52-20261018') == DONE and stage('wasp52-20261108') == FETCHED
    assert (stage('hatp32-20261105'), store.get('hatp32-20261105')['failures']) == (NEW, 0) and stage('hatp32-20261103') == DONE


def test_an_outage_holds_the_autopilot_without_blaming_the_night(tmp_path, monkeypatch):
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    frame = dict(name='WASP-50261026053928', time='2026-10-26T05:39:28+00:00', target='WASP-50', telescope='Cecilia',
                 exposure=60, preview_kb=1, weather=67)
    store.save('wasp50-20261026', stage=NEW, first_frame=frame['time'], listing=dict(frames=[frame], darks=[], entry={}))

    def down(*args, **kwargs):
        raise net.Unreachable('mo-www.cfa.harvard.edu, ConnectionError')
    monkeypatch.setattr(directory, 'download', down)
    pipe.run_once()
    pipe.run_once()
    night = store.get('wasp50-20261026')
    assert pipe.hold == 'offline' and night['stage'] == NEW and night['failures'] == 0
    assert sum('waiting' in row['message'] for row in store.recent(10)) == 1       # said once


def test_one_autopilot_per_data_folder(tmp_path):
    first = claim(tmp_path, 8765)
    assert first and claim(tmp_path, 8766) is None
    assert (tmp_path / 'umbra.lock').read_text() == '8765'


def test_a_looser_gate_fetches_nights_the_old_one_rejected(tmp_path):
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    for nid, clouded in (('wasp52-20261018', 3), ('tres5-20261020', 6)):
        report = dict(result(frames=8, sky=dict(clouded=clouded)), sampled=True)
        store.save(nid, stage=DONE, grade=grade.LOST, headline='Cloud through the night', cleaned=1,
                   first_frame='2026-10-18T03:00:00+00:00', report=report)
    pipe.requeue()
    again, still = store.get('wasp52-20261018'), store.get('tres5-20261020')
    assert (again['stage'], again['grade'], again['cleaned']) == ('sampled', None, 0) and still['stage'] == DONE


def test_a_newer_exotic_gets_the_weak_nights_again(tmp_path, monkeypatch):
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    weak = dict(sky={}, rules=screen.RULES, phot={}, fit=dict(read=reduce.READ, depth=0.0086, qc=None))
    sound = dict(weak, fit=dict(read=reduce.READ, depth=0.0157, qc='PASS, KTMF 4.00 / 5.00'))
    for nid, report in (('wasp50-20261024', weak), ('wasp50-20261028', sound)):
        (pipe.folder(nid) / 'frame.FITS.gz').write_bytes(b'x')
        store.save(nid, stage=DONE, grade=grade.IMPROVABLE, first_frame='2026-10-24T06:45:36+00:00', report=report)
    monkeypatch.setattr(reduce, 'command', lambda: ['exotic'])
    monkeypatch.setattr(reduce, 'newer', lambda cmd: True)
    pipe.requeue()
    assert store.get('wasp50-20261024')['stage'] == SCREENED and store.get('wasp50-20261028')['stage'] == DONE


def test_a_planet_exoclock_cannot_predict_does_not_stop_the_others(tmp_path, monkeypatch):
    def run(target, start):
        stamp = lambda k: '2610%02d%02d%02d00' % (start, 4 + k // 20, 3 * (k % 20))
        return [directory.Frame(target + stamp(k), datetime.strptime(stamp(k), '%y%m%d%H%M%S').replace(tzinfo=timezone.utc),
                                target, 'Cecilia', 60.0) for k in range(60)]
    whole = dict(name='Qatar-1b', star='Qatar-1', ra_j2000='20:13:31.61', dec_j2000='+65:09:43.4', ephem_mid_time=2456234.10322,
                 ephem_period=1.4200242, duration_hours=1.6, depth_r_mmag=23.0)
    other = [directory.Frame('Jupiter261012040000', datetime(2026, 10, 12, 4, tzinfo=timezone.utc), 'Jupiter', 'Ed', 2.0)]
    asked = []
    monkeypatch.setattr(directory, 'listing', lambda days: asked.append(days) or run('WASP-10', 12) + run('Qatar-1', 14) + other)
    monkeypatch.setattr(ephemeris, 'load', lambda cache: {'WASP-10b': dict(whole, name='WASP-10b', duration_hours=None), 'Qatar-1b': whole})
    settings = Settings(data=tmp_path)
    store = Store(settings.db_path)
    pipe = Pipeline(settings, store)
    pipe.refresh()
    pipe.refresh()
    assert asked == [30, 10]                                    # the whole directory first, then the recent part
    assert store.get('wasp10-20261012')['headline'] == 'No ephemeris for this planet'
    assert store.get('qatar1-20261014')['planet'] == 'Qatar-1b'
    assert len(store.all()) == 2                                # a few frames of something else are not a transit run
