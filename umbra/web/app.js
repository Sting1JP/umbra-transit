/* The screen. Reads /api/state and /api/night/<id>, or an inlined snapshot. No dependencies. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const snap = window.APP_SNAPSHOT;
  const getJSON = url => fetch(url, {cache: 'no-store'}).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  const api = {
    state: () => snap ? Promise.resolve(snap.state) : getJSON('api/state'),
    night: id => snap ? Promise.resolve(snap.nights[id]) : getJSON('api/night/' + encodeURIComponent(id)),
    log: () => snap ? Promise.resolve(snap.log) : getJSON('api/log'),
    act: (what, id) => fetch('api/' + what + '/' + encodeURIComponent(id), {method: 'POST', headers: {'X-Local-App': '1'}}).then(r => r.ok),
    uploads: () => fetch('api/uploads', {method: 'POST', headers: {'X-Local-App': '1'}}).then(r => r.json()).then(d => d.found),
    form: () => fetch('api/form', {method: 'POST', headers: {'X-Local-App': '1'}}).then(r => r.json()).then(d => d.ok),
  };

  const WORD = {good: 'Good', improvable: 'Improvable', lost: 'Lost', waiting: 'Waiting'};
  const HOLD = {offline: 'Offline. Trying again soon', busy: 'The server is busy. Trying again later', disk: 'Disk nearly full. Downloads paused'};
  const STAGE = {directory: 'Reading the directory', sample: 'Sampling', fetch: 'Downloading', screen: 'Measuring', reduce: 'Reducing', fit: 'Fitting the transit'};
  const ID = /^[a-z0-9]+-\d{8}$/;
  const calm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  let state = null, selected = null, detail = null, heroKey = '', stripKey = '', uid = 0, minimal = false;
  try { minimal = localStorage.getItem('view') === 'minimal'; } catch (e) { /* storage unavailable */ }
  let follow = !ID.test(location.hash.slice(1));     // show the night being worked on until the user picks one

  const ms = iso => Date.parse(iso);
  const hhmm = iso => new Date(ms(iso)).toISOString().slice(11, 16);
  const day = (iso, year) => new Date(ms(iso)).toLocaleDateString('en-GB',
    Object.assign({day: 'numeric', month: 'short', timeZone: 'UTC'}, year ? {year: 'numeric'} : {}));
  const planet = n => (n.planet || n.target).replace(/([0-9])([A-D]?)([b-z])$/, (all, digit, star, letter) => digit + (star && ' ' + star) + ' ' + letter);
  const pct = (v, digits) => (v * 100).toFixed(digits == null ? 1 : digits);
  const gradeOf = n => n.grade || 'waiting';
  const f = v => v.toFixed(1);

  // ---------------------------------------------------------------- top line
  const TICK = '<svg class="tick" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.5l2.8 2.8L10 3.4"/></svg>';
  const span = s => s >= 5400 ? Math.round(s / 3600) + ' h' : Math.max(1, Math.round(s / 60)) + ' min';
  const pending = () => state.nights.filter(n => n.grade === 'good' && !n.uploaded);     // good nights still to upload
  const complete = () => !state.activity && !HOLD[state.hold] && state.nights.length > 0 && state.nights.every(n => n.stage === 'done');

  function statusText() {
    const a = state.activity, total = state.nights.length, left = state.nights.filter(n => n.stage !== 'done').length;
    if (a) {
      const n = state.nights.find(x => x.id === a.night);
      return '<b>' + esc(STAGE[a.stage] || a.stage) + '</b>' + (n ? ' ' + esc(planet(n)) + ', ' + day(n.first_frame) : '') +
        (a.total ? ' <span class="num">' + a.done + '/' + a.total + '</span>' : '');
    }
    if (HOLD[state.hold]) return HOLD[state.hold];
    if (left) return left + (left === 1 ? ' night' : ' nights') + ' queued';
    if (!total) return 'Up to date';
    return TICK + '<b>All <span class="num">' + total + '</span> nights done</b>' +
      (state.next_look != null ? '<span class="of">Next check in ' + span(state.next_look) + '</span>' : '');
  }

  function renderBar() {
    const a = state.activity, count = g => state.nights.filter(n => gradeOf(n) === g).length;
    const total = state.nights.length, done = state.nights.filter(n => n.stage === 'done').length, all = complete();
    $('wordmark').textContent = state.name;
    retitle();
    $('bar').classList.toggle('busy', !!a);
    $('bar').classList.toggle('complete', all);
    const share = total ? (done + (a && a.total ? a.done / a.total : 0)) / total : 0;
    $('status').innerHTML = '<span class="label">Autopilot</span>' + statusText() +
      (total && !all ? '<span class="of"><span class="num">' + done + '</span> of <span class="num">' + total + '</span> nights screened</span>' : '');
    $('progress').style.width = (share * 100).toFixed(1) + '%';
    const sent = state.nights.filter(n => n.uploaded).length, ready = pending().length;
    $('tally').innerHTML = ['good', 'improvable', 'lost', 'waiting'].map(g =>
      '<span><i class="mini ' + g + '"></i><span class="num">' + count(g) + '</span> ' + WORD[g].toLowerCase() + '</span>').join('') +
      (sent ? '<span><span class="num">' + sent + '</span> uploaded</span>' : '') +
      (ready && !snap ? '<button class="go small" data-act="upload" title="Upload the good nights (U)">Upload <span class="num">' + ready + '</span><span>→</span></button>' : '');
    $('version').textContent = 'v' + state.version + (state.snapshot ? ' · snapshot ' + day(state.snapshot) + ', ' + hhmm(state.snapshot) + ' UT' : '');
  }

  // ---------------------------------------------------------------- thirty nights
  function renderStrip() {
    const live = state.activity && state.activity.night;
    const key = selected + '|' + live + '|' + state.nights.map(n => n.id + gradeOf(n) + (n.uploaded ? 'u' : '')).join();
    if (key === stripKey) return;
    if (stripKey) $('strip').classList.remove('boot');       // bars grow once, on first paint
    stripKey = key;
    let month = '';
    const at = state.nights.findIndex(n => n.id === selected);
    $('strip').innerHTML = state.nights.map((n, i) => {
      const d = new Date(ms(n.first_frame)), g = gradeOf(n), m = d.toLocaleDateString('en-GB', {month: 'short', timeZone: 'UTC'});
      const head = m !== month ? '<em>' + m + '</em>' : '';
      month = m;
      const on = i === at, tick = on || (i % 4 === 0 && Math.abs(i - at) > 1);
      return '<button class="n ' + (n.id === live ? 'live' : g) + (on ? ' on' : '') + (n.uploaded ? ' up' : '') + '" data-id="' + esc(n.id) + '" title="' +
        esc(planet(n) + ' · ' + day(n.first_frame) + ' · ' + WORD[g].toLowerCase() + (n.uploaded ? ', uploaded' : '')) + '">' + head + '<i></i>' +
        (tick ? '<b>' + d.getUTCDate() + '</b>' : '') + '</button>';
    }).join('');
    $('strip').querySelectorAll('.n i').forEach((el, i) => el.style.setProperty('--i', i));
    const soon = state.nights.filter(n => gradeOf(n) === 'waiting' && n.days_left < 4).length;
    $('month-title').textContent = state.nights.length + ' nights';
    $('month-note').textContent = soon ? soon + ' leaving soon' : 'select one';
  }

  // ---------------------------------------------------------------- tone: the star's colour, drained when the night is lost
  // A stylised ramp ordered by real effective temperature (K); true star colours are far paler.
  const RAMP = [[3800, [255, 136, 50]], [4700, [255, 164, 68]], [5400, [255, 194, 108]], [6000, [255, 220, 158]], [6600, [255, 238, 208]], [7600, [226, 234, 255]]];
  const mix = (a, b, k) => a.map((v, i) => v + (b[i] - v) * k);
  const rgb = c => 'rgb(' + c.map(Math.round).join(',') + ')';
  function tone(tr, grade) {
    const t = Math.min(7600, Math.max(3800, (tr && tr.teff) || 5200));
    let j = 1;
    while (RAMP[j][0] < t) j++;
    const c = mix(RAMP[j - 1][1], RAMP[j][1], (t - RAMP[j - 1][0]) / (RAMP[j][0] - RAMP[j - 1][0]));
    return grade === 'lost' ? mix(c, [150, 150, 156], 0.72) : c;
  }
  const GLOW = {good: 0.36, improvable: 0.28, waiting: 0.2, lost: 0.08};

  // ---------------------------------------------------------------- the star, the planet and its path, to scale
  function disk(tr) {                       // the planet to scale on its chord; the star itself is painted behind, see renderHero
    const R = 64, k = tr.rp_rs || Math.sqrt(tr.depth), b = Math.min(0.95, tr.impact || 0), y = f(-b * R), r = f(Math.max(2.5, k * R));
    return '<div class="orb"><canvas></canvas><svg viewBox="-110 -110 220 220" role="img" aria-label="Star and planet to scale">' +
      '<line class="path" x1="-108" x2="108" y1="' + y + '" y2="' + y + '"/>' +
      '<circle class="planet" id="planet" r="' + r + '" cy="' + y + '" cx="0"/></svg></div><span class="label num">R<sub>p</sub>/R<sub>s</sub> ' + k.toFixed(3) + (tr.impact != null ? ' · b ' + b.toFixed(2) : '') + (tr.teff ? ' · ' + Math.round(tr.teff) + ' K' : '') + (tr.source && tr.source !== 'ExoClock' ? ' · ' + esc(tr.source) : '') + '</span>';
  }

  // ---------------------------------------------------------------- focus
  function stats(n, r) {
    const tr = r.transit, p = r.phot, sky = r.sky, air = tr && tr.airmass;
    const airmass = air ? ['Airmass', air[0].toFixed(2) + '–' + air[1].toFixed(2), ''] : null;
    const window_ = tr ? ['Transit', hhmm(tr.ingress) + '–' + hhmm(tr.egress), 'UT'] : null;
    if (r.fit) {
      const oc = (r.fit.tc_bjd - tr.tc_bjd) * 1440;
      return [['Depth', pct(r.fit.depth, 2), '% of ' + pct(r.fit.expected || (tr.rp_rs ? tr.rp_rs * tr.rp_rs : tr.depth), 2)], ['Timing', (oc >= 0 ? '+' : '−') + Math.abs(oc).toFixed(1), 'min'],
              ['Scatter', pct(r.fit.scatter), '%'], airmass];
    }
    if (p && p.depth != null) {
      return [['Depth', pct(p.depth), '% of ' + pct(tr.depth)], ['Signal', p.snr.toFixed(1), 'σ'], ['Scatter', pct(p.scatter), '%'], airmass];
    }
    if (sky && r.sampled) {
      return [['Sample', (r.frames - sky.clouded) + '/' + r.frames, 'clear'], window_, airmass, ['Leaves in', Math.max(0, Math.ceil(n.days_left)), 'd']];
    }
    if (sky) {
      return [['Clear', (r.frames - sky.clouded) + '/' + r.frames, 'frames'], ['In transit', (sky.in_transit - sky.clouded_in_transit) + '/' + sky.in_transit, 'clear'],
              ['Missing', Math.round(r.gap.in_transit_min), 'min'], airmass];
    }
    return [['Frames', n.frames, ''], window_, tr ? ['Baseline', Math.max(0, Math.round(tr.pre_min)) + '/' + Math.max(0, Math.round(tr.post_min)), 'min'] : null,
            ['Leaves in', Math.max(0, Math.ceil(n.days_left)), 'd']];
  }

  const stat = s => '<div class="stat"><div class="label">' + esc(s[0]) + '</div><div class="v num">' + esc(s[1]) +
    (s[2] ? '<small>' + esc(s[2]) + '</small>' : '') + '</div></div>';

  function sentence(n, r) {
    const v = r.verdict, g = gradeOf(n), a = state.activity;
    if (a && a.night === n.id && n.stage !== 'done') {
      return '<span class="g live-t">In progress</span><span class="why">' + esc(STAGE[a.stage] || a.stage) +
        (a.total ? ' <span class="num">' + a.done + '</span> of <span class="num">' + a.total + '</span> frames' : '') + '.</span>';
    }
    const why = v ? v.headline : (n.headline || (n.stage === 'new' ? 'Not looked at yet' : 'In progress'));
    return '<span class="g ' + g + '-t">' + WORD[g] + '</span><span class="why">' + esc(why) + '.</span>' +
      (v && v.remedy ? ' ' + esc(v.remedy) + '.' : '') + (n.uploaded ? ' Uploaded ' + day(n.uploaded) + '.' : '');
  }

  function renderHero(animate) {
    const i = state.nights.findIndex(x => x.id === selected), n = state.nights[i];
    if (!n || !detail) { $('hero').innerHTML = '<p class="label">No nights yet</p>'; return; }
    const r = detail.report || {}, g = gradeOf(n), c = tone(r.transit, g), live = state.activity && state.activity.night;
    const fit = getComputedStyle($('app')).getPropertyValue('--fit').trim() === '1';
    $('hero').style.setProperty('--tone', rgb(c));
    $('hero').classList.toggle('still', !animate);
    $('hero').innerHTML =
      '<div class="top"><div class="when"><span class="label">' + day(n.first_frame, true) + '<span class="hrs"> · <span class="num">' + hhmm(n.first_frame) + '–' + hhmm(n.last_frame) + '</span> UT</span></span>' +
      '<button class="nav" data-step="-1" aria-label="Previous night"' + (i ? '' : ' disabled') + '>‹</button>' +
      '<button class="nav" data-step="1" aria-label="Next night"' + (i < state.nights.length - 1 ? '' : ' disabled') + '>›</button>' +
      (live && live !== n.id ? '<button class="golive" id="go-live">Jump to in progress<span>→</span></button>' : '') + '</div>' +
      '<h1 class="name">' + esc(planet(n)) + '</h1><p class="say" id="say">' + sentence(n, r) + '</p>' +
      (r.transit ? '<div class="disk ' + g + '">' + disk(r.transit) + '</div>' : '') + '</div>' +
      '<div class="chart" id="chart"></div>' +
      '<div class="stats">' + stats(n, r).filter(Boolean).map(stat).join('') +
      (r.verdict ? '<button class="go" id="open-report">This night\'s report<span>→</span></button>' : '') + '</div>';
    const g0 = draw($('chart'), n, r, animate && !calm, !calm, fit), tr = r.transit, orb = $('hero').querySelector('.orb canvas');
    if (orb) {                                // the same star as the minimal view
      const side = orb.parentNode.clientWidth, R = 64 / 220 * side * (window.devicePixelRatio || 1), art = sized(orb, side, side);
      paintHalo(art, c, R, GLOW[g] / GLOW.good);
      paintStar(art, c, R, n.id);
    }
    scene = null;
    if (tr && !calm && g0.curve) {            // the night replays only once there is a light curve
      const head = $('playhead'), dot = $('playdot'), body = $('planet'), cross = crossing(r, 64);
      scene = {start: performance.now(), t0: g0.t0, t1: g0.t1, alive: () => head.isConnected,
               step(u, t) {
                 head.setAttribute('transform', 'translate(' + f(u * g0.W) + ' 0)');
                 dot.setAttribute('cy', f(g0.curve(t)));
                 body.setAttribute('cx', f(cross(t)));
               }};
    }
  }

  // ---------------------------------------------------------------- what a night's curve is made of
  function pointsOf(r) {                    // [time, flux]: EXOTIC's points if it ran, else the quick look's
    const fc = r.fit && r.fit.curve, s = r.series;
    const pts = fc ? fc.t.map((t, i) => [ms(t), fc.flux[i]])
      : (r.phot && r.phot.rel ? s.t.map((t, i) => [ms(t), r.phot.rel[i]]).filter(e => e[1] != null) : []);
    return pts.length ? pts : null;
  }

  function modelOf(r) {                     // the fitted model, or the predicted transit when there is no fit
    const tr = r.transit, fc = r.fit && r.fit.curve, a = ms(tr.ingress), b = ms(tr.egress), k = (b - a) * 0.12, deep = 1 - tr.depth;
    const m = fc ? fc.t.map((t, i) => [ms(t), fc.model[i]]).sort((u, v) => u[0] - v[0]) : [[a, 1], [a + k, deep], [b - k, deep], [b, 1]];
    return {points: m, at(t) {              // between its points, so whatever follows it glides
      if (t <= m[0][0] || t >= m[m.length - 1][0]) return 1;
      let j = 0;
      while (m[j + 1][0] < t) j++;
      return m[j][1] + (m[j + 1][1] - m[j][1]) * (t - m[j][0]) / (m[j + 1][0] - m[j][0] || 1);
    }};
  }

  function crossing(r, R) {                 // the planet's place along its chord at time t, for a star of radius R; 0 is mid-transit
    const tr = r.transit, late = r.fit ? (r.fit.tc_bjd - tr.tc_bjd) * 864e5 : 0;     // it keeps the measured time, when there is one
    const k = tr.rp_rs || Math.sqrt(tr.depth), b = Math.min(0.95, tr.impact || 0), a = ms(tr.ingress) + late, z = ms(tr.egress) + late;
    const reach = R * Math.sqrt(Math.max(0.01, (1 + k) * (1 + k) - b * b));
    return t => reach * (2 * (t - a) / (z - a) - 1);
  }

  // ---------------------------------------------------------------- chart: light curve over the sky strip, one time axis
  function draw(el, n, r, animate, live, fit) {
    const tr = r.transit, s = r.series, fc = r.fit && r.fit.curve, p = tr && pointsOf(r);
    const W = Math.max(280, Math.round(el.clientWidth)), wide = W > 560;
    const top = 20, axis = 26, gap = p ? 22 : 0, room = fit ? el.clientHeight - top - axis : 0;
    let hLc = p ? Math.round(Math.min(340, Math.max(200, W * 0.3))) : 0;
    let hSky = s ? (p ? 44 : Math.round(Math.min(190, Math.max(120, W * 0.16)))) : 40;
    if (room > 0) {                         // one-screen layout: fill the space the page gives the chart
      if (p && room < 230) hSky = 30;
      if (p) hLc = Math.max(60, room - gap - hSky);
      else hSky = Math.max(50, room);
    }
    const H = top + hLc + gap + hSky + axis;
    const t0 = ms(n.first_frame) - 6e5, t1 = ms(n.last_frame) + 6e5, x = t => (t - t0) / (t1 - t0) * W;
    const clip = 'c' + (++uid);
    let back = '', data = '', curve = null;

    if (tr) {
      const a = x(ms(tr.ingress)), b = x(ms(tr.egress));
      back += '<rect class="win" x="' + f(a) + '" y="' + top + '" width="' + f(Math.max(1, b - a)) + '" height="' + (H - axis - top) + '"/>' +
        '<line class="edge" x1="' + f(a) + '" x2="' + f(a) + '" y1="' + (top - 6) + '" y2="' + (H - axis) + '"/>' +
        '<line class="edge" x1="' + f(b) + '" x2="' + f(b) + '" y1="' + (top - 6) + '" y2="' + (H - axis) + '"/>' +
        '<text class="hot" x="' + f(a + 6) + '" y="' + (top - 8) + '">' + hhmm(tr.ingress) + '</text>' +
        '<text class="hot" x="' + f(b - 6) + '" y="' + (top - 8) + '" text-anchor="end">' + hhmm(tr.egress) + '</text>' +
        (b - a > 200 ? '<text class="cap hot" x="' + f((a + b) / 2) + '" y="' + (top - 8) + '" text-anchor="middle">Transit</text>' : '');
    }
    if (p) {
      const vals = p.map(e => e[1]).sort((a, b) => a - b), q = k => vals[Math.floor(k * (vals.length - 1))];
      const lo = Math.min(1 - tr.depth * 1.9, q(0.03)), hi = Math.max(1 + tr.depth, q(0.97));
      const y = v => top + 8 + (hi - Math.min(hi, Math.max(lo, v))) / (hi - lo) * (hLc - 16);
      const perUnit = (hLc - 16) / (hi - lo), step = [0.01, 0.02, 0.05, 0.1].find(v => v * perUnit >= 20) || 0.2;
      for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
        const one = Math.abs(v - 1) < 1e-6;
        back += '<line class="' + (one ? 'rule' : 'tick') + '" x1="0" x2="' + (one ? W : 5) + '" y1="' + f(y(v)) + '" y2="' + f(y(v)) + '"/>' +
          '<text x="9" y="' + f(y(v) + (one ? -5 : 3.5)) + '">' + v.toFixed(2) + '</text>';
      }
      const model = modelOf(r), bins = {};
      curve = t => y(model.at(t));
      data += '<path class="pred" d="M0 ' + f(y(1)) + model.points.map(e => 'L' + f(x(e[0])) + ' ' + f(y(e[1]))).join('') + 'L' + W + ' ' + f(y(1)) + '"/>';
      p.forEach(e => {
        const t = e[0], v = e[1], key = Math.floor((t - t0) / 9e5);
        (bins[key] = bins[key] || []).push([t, v]);
        data += '<circle class="pt" cx="' + f(x(t)) + '" cy="' + f(y(v)) + '" r="1.7"/>';
      });
      const mean = (arr, j) => arr.reduce((sum, e) => sum + e[j], 0) / arr.length;
      Object.keys(bins).forEach(key => {
        if (bins[key].length >= 3) data += '<circle class="bin" cx="' + f(x(mean(bins[key], 0))) + '" cy="' + f(y(mean(bins[key], 1))) + '" r="3.4"/>';
      });
    }
    const y0 = top + hLc + gap, floor = y0 + hSky;
    if (s) {
      const sw = Math.max(1.2, Math.min(3.2, (r.cadence_min || 3) * 6e4 / (t1 - t0) * W * 0.5));
      data += s.t.map((iso, i) => '<line class="sky' + (s.cloudy[i] ? ' c' : '') + '" stroke-width="' + f(sw) + '" x1="' + f(x(ms(iso))) + '" x2="' + f(x(ms(iso))) +
        '" y1="' + f(floor - 1) + '" y2="' + f(floor - 1 - Math.max(1.5, s.clear[i] * (hSky - 14))) + '"/>').join('');
      back += '<text class="cap" x="0" y="' + (y0 + 8) + '">' + (r.sampled ? 'Sky · sample' : 'Sky') + '</text>';
    } else {
      back += '<line class="span" x1="' + f(x(ms(n.first_frame))) + '" x2="' + f(x(ms(n.last_frame))) + '" y1="' + (floor - 12) + '" y2="' + (floor - 12) + '"/>' +
        '<text class="cap" x="0" y="' + (y0 + 8) + '">Frames</text>';
    }
    const hour = 36e5, every = W / ((t1 - t0) / hour) < 56 ? 2 : 1;
    back += '<line class="rule" x1="0" x2="' + W + '" y1="' + (floor + 0.5) + '" y2="' + (floor + 0.5) + '"/>';
    for (let t = Math.ceil(t0 / hour) * hour; t < t1; t += hour) {
      const hh = new Date(t).getUTCHours();
      back += '<line class="tick" x1="' + f(x(t)) + '" x2="' + f(x(t)) + '" y1="' + floor + '" y2="' + (floor + 4) + '"/>';
      if (hh % every === 0 && x(t) > 20 && x(t) < W - 30) back += '<text x="' + f(x(t)) + '" y="' + (H - 6) + '" text-anchor="middle">' + String(hh).padStart(2, '0') + ':00</text>';
    }
    back += '<text x="' + W + '" y="' + (H - 6) + '" text-anchor="end">UT</text>';
    const wipe = '<clipPath id="' + clip + '"><rect x="-6" y="0" height="' + H + '" width="' + (W + 12) + '">' +
      (animate ? '<animate attributeName="width" values="0;' + (W + 12) + '" dur="1.25s" calcMode="spline" keyTimes="0;1" keySplines=".2 .8 .2 1" fill="freeze"/>' : '') +
      '</rect></clipPath>';
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="' +
      (p ? (fc ? 'Fitted light curve' : 'Light curve') + ' and sky transparency' : 'Sky transparency') + ' through the night"><defs>' + wipe + '</defs>' + back +
      '<g clip-path="url(#' + clip + ')">' + data + '</g>' +
      (live && curve ? '<g id="playhead"><line class="ph" y1="' + top + '" y2="' + (H - axis) + '"/><circle class="phd" id="playdot" r="3.6"/></g>' : '') + '</svg>';
    return {t0: t0, t1: t1, W: W, curve: curve};
  }

  // ---------------------------------------------------------------- playback: the night replays slowly, planet and playhead in step
  const PERIOD = 32000;
  let scene = null;
  function frame(now) {                     // puts the scene where the replay is at this moment
    if (!scene || !scene.alive() || scene.over) return;
    const laps = Math.max(0, now - scene.start) / PERIOD, u = laps % 1;       // a scene can be made after this frame's clock was read
    if (laps >= 1 && scene.next) {
      scene.over = true;                    // asked once: the next night takes a few frames to load
      scene.next();
    } else {
      scene.step(u, scene.t0 + u * (scene.t1 - scene.t0));
    }
  }

  function play(now) {
    frame(now);
    requestAnimationFrame(play);
  }
  if (!calm) requestAnimationFrame(play);

  // ---------------------------------------------------------------- minimal view: the star, its planet and the night's curve
  const TURN = 2 * Math.PI, rgba = (c, a) => 'rgba(' + c.map(Math.round).join(',') + ',' + a + ')';
  const clamp = v => Math.min(1, Math.max(0, v));

  function seeded(text) {                   // a small repeatable random source, so each night keeps its own sky
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
    return () => {
      h = h + 0x6D2B79F5 | 0;
      let t = Math.imul(h ^ h >>> 15, 1 | h);
      t ^= t + Math.imul(t ^ t >>> 7, 61 | t);
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  function sized(canvas, w, h) {            // CSS size w x h, drawn at the screen's own resolution
    const px = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * px);
    canvas.height = Math.round(h * px);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    return canvas.getContext('2d');
  }

  function glow(g, x, y, r, inner, outer) {
    const fill = g.createRadialGradient(x, y, 0, x, y, r);
    fill.addColorStop(0, inner);
    fill.addColorStop(1, outer);
    g.fillStyle = fill;
    g.fillRect(x - r, y - r, 2 * r, 2 * r);
  }

  function paintSky(g, seed) {
    const w = g.canvas.width, h = g.canvas.height, px = window.devicePixelRatio || 1, rnd = seeded(seed);
    g.fillStyle = '#050608';
    g.fillRect(0, 0, w, h);
    for (let i = 0; i < 46; i++) {          // a faint band of dust across the field
      const along = rnd();
      glow(g, w * along + (rnd() - 0.5) * w * 0.3, h * (0.82 - 0.64 * along) + (rnd() - 0.5) * h * 0.34, (0.1 + 0.2 * rnd()) * w,
           'rgba(120,140,190,0.022)', 'rgba(120,140,190,0)');
    }
    for (let i = 0, n = Math.round(w * h / (px * px) / 380); i < n; i++) {
      const x = rnd() * w, y = rnd() * h, m = Math.pow(rnd(), 7), size = (0.3 + 1.1 * m) * px;       // many faint, very few bright
      const c = mix(tone({teff: 3600 + 4200 * rnd()}), [255, 255, 255], 0.62);
      if (m > 0.7) glow(g, x, y, size * 6, rgba(c, 0.2 * m), rgba(c, 0));
      if (m > 0.975) {                      // the brightest few get the faint cross a telescope gives them
        g.strokeStyle = rgba(c, 0.22);
        g.lineWidth = 0.6 * px;
        g.beginPath();
        g.moveTo(x - size * 14, y); g.lineTo(x + size * 14, y);
        g.moveTo(x, y - size * 14); g.lineTo(x, y + size * 14);
        g.stroke();
      }
      g.fillStyle = rgba(c, 0.16 + 0.84 * Math.pow(m, 0.55));
      g.beginPath();
      g.arc(x, y, size, 0, TURN);
      g.fill();
    }
    const grain = document.createElement('canvas'), speck = grain.getContext('2d');      // fine grain, so the soft gradients do not band
    grain.width = grain.height = 128;
    for (let i = 0; i < 5000; i++) {
      speck.fillStyle = rnd() < 0.5 ? 'rgba(255,255,255,0.016)' : 'rgba(0,0,0,0.05)';
      speck.fillRect(rnd() * 128, rnd() * 128, 1, 1);
    }
    g.fillStyle = g.createPattern(grain, 'repeat');
    g.fillRect(0, 0, w, h);
    const shade = g.createRadialGradient(w / 2, h * 0.45, Math.min(w, h) * 0.3, w / 2, h * 0.45, Math.hypot(w, h) * 0.62);
    shade.addColorStop(0, 'rgba(0,0,0,0)');
    shade.addColorStop(1, 'rgba(0,0,0,0.6)');
    g.fillStyle = shade;
    g.fillRect(0, 0, w, h);
  }

  function paintHalo(g, c, R, strength) {  // the glow around a star of radius R (canvas pixels) at the middle of a square canvas
    const mid = g.canvas.width / 2, halo = g.createRadialGradient(mid, mid, R * 0.96, mid, mid, mid);
    [[0, 0.34], [0.12, 0.13], [0.45, 0.035], [1, 0]].forEach(s => halo.addColorStop(s[0], rgba(c, s[1] * strength)));
    g.fillStyle = halo;
    g.fillRect(0, 0, 2 * mid, 2 * mid);
  }

  const mottle = {};                        // the last night's granulation: made once, then stretched to any size
  function granulation(seed) {              // a fine mottling that fades toward the limb
    if (mottle.seed === seed) return mottle.tile;
    const tile = document.createElement('canvas'), g = tile.getContext('2d'), R = 256, rnd = seeded(seed + 'star');
    tile.width = tile.height = 2 * R;
    for (let i = 0, n = Math.round(R * R / 7); i < n; i++) {
      const a = rnd() * TURN, d = Math.sqrt(rnd()) * R, fade = 1 - d / R;
      g.fillStyle = rnd() < 0.5 ? 'rgba(255,255,255,' + 0.035 * fade + ')' : 'rgba(40,10,0,' + 0.03 * fade + ')';
      g.beginPath();
      g.arc(R + d * Math.cos(a), R + d * Math.sin(a), R * (0.005 + 0.012 * rnd()), 0, TURN);
      g.fill();
    }
    mottle.seed = seed;
    return mottle.tile = tile;
  }

  function paintStar(g, c, R, seed) {       // the disk itself, at the middle of a square canvas
    const mid = g.canvas.width / 2;
    const hot = mix(c, [255, 255, 255], 0.86), deep = mix(c, [44, 9, 0], 0.64);
    const body = g.createRadialGradient(mid, mid, 0, mid, mid, R);
    for (let i = 0; i <= 16; i++) {         // limb darkening: dimmer and redder toward the edge
      const r = i / 16, light = 1 - 0.8 * (1 - Math.sqrt(1 - r * r));
      body.addColorStop(r, rgb(mix(deep, hot, Math.pow(light, 1.3))));
    }
    g.fillStyle = body;
    g.beginPath();
    g.arc(mid, mid, R, 0, TURN);
    g.fill();
    g.save();
    g.clip();
    g.drawImage(granulation(seed), mid - R, mid - R, 2 * R, 2 * R);
    g.restore();
  }

  function paintPlanet(g, r, ux, uy, lit, c) {      // its night side; off the star's disk, a thin crescent on the side facing the star
    const mid = g.canvas.width / 2;
    g.clearRect(0, 0, 2 * mid, 2 * mid);
    if (lit > 0.01) {
      const x = mid + ux * r * 0.09, y = mid + uy * r * 0.09, rim = g.createRadialGradient(x, y, r * 0.88, x, y, r * 1.07);
      rim.addColorStop(0, rgba(c, 0));
      rim.addColorStop(0.5, rgba(mix(c, [255, 255, 255], 0.5), 0.9 * lit));
      rim.addColorStop(1, rgba(c, 0));
      g.fillStyle = rim;
      g.beginPath();
      g.arc(x, y, r * 1.07, 0, TURN);
      g.fill();
    }
    g.fillStyle = '#020203';
    g.beginPath();
    g.arc(mid, mid, r, 0, TURN);
    g.fill();
  }

  function trace(el, n, r) {                // the night's curve as one thin line, drawn up to a moving edge while it plays
    const tr = r.transit, model = modelOf(r), pts = pointsOf(r), w = el.clientWidth, h = 84, clip = 'u' + (++uid);
    const t0 = ms(n.first_frame) - 6e5, t1 = ms(n.last_frame) + 6e5, lo = 1 - tr.depth * 2.2, hi = 1 + tr.depth * 1.2;
    const x = t => (t - t0) / (t1 - t0) * w, y = v => 6 + (hi - Math.min(hi, Math.max(lo, v))) / (hi - lo) * (h - 12);
    // before there is a curve: the predicted dip, dotted, over one tick for each frame of the night
    const frames = pts ? '' : Array.from({length: n.frames}, (_, i) => f(x(ms(n.first_frame) + (i + 0.5) / n.frames * (ms(n.last_frame) - ms(n.first_frame)))))
      .map(at => '<line class="fr" x1="' + at + '" x2="' + at + '" y1="' + (h - 9) + '" y2="' + (h - 3) + '"/>').join('');
    el.innerHTML = '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '" role="img" aria-label="Light curve of the night">' +
      '<clipPath id="' + clip + '"><rect width="' + w + '" height="' + h + '"/></clipPath>' +
      '<line class="rule" x1="0" x2="' + w + '" y1="' + f(y(1)) + '" y2="' + f(y(1)) + '"/>' +
      [tr.ingress, tr.egress].map(t => '<line class="edge" x1="' + f(x(ms(t))) + '" x2="' + f(x(ms(t))) + '" y1="2" y2="' + (h - 2) + '"/>').join('') +
      frames + '<g clip-path="url(#' + clip + ')">' + (pts || []).map(e => '<circle class="pt" cx="' + f(x(e[0])) + '" cy="' + f(y(e[1])) + '" r="1.4"/>').join('') +
      '<path class="pred' + (pts ? '' : ' soon') + '" d="M0 ' + f(y(1)) + model.points.map(e => 'L' + f(x(e[0])) + ' ' + f(y(e[1]))).join('') + 'L' + w + ' ' + f(y(1)) + '"/></g>' +
      (calm ? '' : '<circle class="phd" r="4"/>') + '</svg>';
    return {t0: t0, t1: t1, x: x, y: y, flux: model.at, dot: el.querySelector('.phd'), edge: el.querySelector('clipPath rect')};
  }

  function renderAmbient(fresh) {
    const n = state.nights.find(x => x.id === selected), box = $('ambient'), old = Array.from(box.children);
    scene = null;
    if (!n || !detail) { box.innerHTML = ''; return; }
    const r = detail.report || {}, tr = r.transit, g = gradeOf(n), c = tone(tr, g);
    const view = document.createElement('div');
    view.className = 'a-scene';
    view.style.setProperty('--tone', rgb(c));
    view.innerHTML = '<canvas class="a-sky"></canvas><canvas></canvas><canvas></canvas><canvas></canvas>' +
      '<div class="a-top"><div class="mark">' + $('bar').querySelector('.mark').innerHTML + '</div>' +
      '<span class="a-status"></span><span class="tools">' + $('tools').innerHTML + '</span></div>' +
      '<div class="a-foot"><div class="a-who"><div><span class="label">' + day(n.first_frame, true) + ' · <span class="a-state ' + g + '-t">' + WORD[g] + '</span></span>' +
      '<h1 class="name">' + esc(planet(n)) + '</h1></div><div class="a-stats">' + stats(n, r).filter(Boolean).slice(0, 2).map(stat).join('') + '</div></div>' +
      '<div class="chart a-trace"></div></div>';
    box.appendChild(view);
    if (fresh && old.length && !calm) {      // a new night fades in over the last one
      view.classList.add('arrive');
      setTimeout(() => old.forEach(e => e.remove()), 1300);
    } else {
      old.forEach(e => e.remove());
    }
    const [sky, halo, star, body] = view.querySelectorAll('canvas'), W = box.clientWidth, H = box.clientHeight, px = window.devicePixelRatio || 1;
    const R = Math.min(W, H) * 0.215, cx = W / 2, cy = H * 0.41, wide = R * 5.2, tight = 2 * R + 4;
    const k = tr ? tr.rp_rs || Math.sqrt(tr.depth) : 0.1, pr = Math.max(3, k * R), room = pr * 3;
    const at = (canvas, x, y, side) => { canvas.style.transform = 'translate(' + f(x - side / 2) + 'px,' + f(y - side / 2) + 'px)'; };
    paintSky(sized(sky, W, H), n.id);
    paintHalo(sized(halo, wide, wide), c, R * px, GLOW[g] / GLOW.good);
    paintStar(sized(star, tight, tight), c, R * px, n.id);
    at(halo, cx, cy, wide);
    at(star, cx, cy, tight);
    const night = sized(body, room, room), cross = tr ? crossing(r, R) : () => 0, py = cy - (tr ? Math.min(0.95, tr.impact || 0) : 0) * R;
    const place = t => {
      const x = cx + cross(t), dx = cx - x, dy = cy - py, far = Math.hypot(dx, dy) || 1;
      const edge = clamp((far - (R - pr)) / (2 * pr)), lit = edge * edge * (3 - 2 * edge) * clamp(1 - (far - R) / (1.4 * R));
      at(body, x, py, room);
      paintPlanet(night, pr * px, dx / far, dy / far, lit, c);
    };
    // a night still in work has no curve yet: its predicted transit plays instead
    const line = tr && (pointsOf(r) || !n.grade) ? trace(view.querySelector('.a-trace'), n, r) : null;
    if (line && line.dot) {
      scene = {start: performance.now(), t0: line.t0, t1: line.t1, alive: () => line.dot.isConnected, next: advance,
               step(u, t) {
                 const flux = line.flux(t), x = f(line.x(t));
                 line.dot.setAttribute('cx', x);
                 line.dot.setAttribute('cy', f(line.y(flux)));
                 line.edge.setAttribute('width', x);
                 halo.style.opacity = (1 - Math.min(0.45, 12 * (1 - flux))).toFixed(3);      // the glow dips with the light, a few times over
                 place(t);
               }};
    }
    place(scene ? line.t0 : tr ? (ms(tr.ingress) + ms(tr.egress)) / 2 : 0);     // parked mid-transit when nothing plays
    ambientStatus();
  }

  function ambientStatus() {
    const view = $('ambient').querySelector('.a-scene:last-child'), n = state.nights.find(x => x.id === selected);
    if (!view) return;
    view.querySelector('.a-status').innerHTML = statusText();
    view.querySelector('.a-top').classList.toggle('busy', !!state.activity);
    if (!n || n.grade) return;
    const a = state.activity, live = a && a.night === n.id ? a : null;      // a night in work: what is being done, and the frames in hand
    const here = live && live.stage === 'fetch' ? live.done : (live && live.stage !== 'sample') || n.stage === 'fetched' ? n.frames : 0;
    const word = view.querySelector('.a-state');
    word.textContent = live ? STAGE[live.stage] + (live.total ? ' ' + live.done + '/' + live.total : '') : 'Queued';
    word.classList.toggle('on', !!live);
    view.querySelectorAll('.fr').forEach((tick, i) => tick.classList.toggle('in', i < here));
  }

  function advance() {                      // one replay each, through the nights that have a curve and the one in work
    const live = state.activity && state.activity.night;
    const pool = state.nights.filter(n => n.id === live || (n.stage === 'done' && (n.grade === 'good' || n.grade === 'improvable'))).map(n => n.id);
    const next = pool[(pool.indexOf(selected) + 1) % pool.length];
    if (next && next !== selected) return select(next);
    scene.start = performance.now();        // the only one: it plays again
    scene.over = false;
  }

  function setMinimal(on) {
    minimal = on;
    try { localStorage.setItem('view', on ? 'minimal' : 'full'); } catch (e) { /* storage unavailable */ }
    $('app').hidden = on;
    $('ambient').hidden = !on;
    if (!on) $('ambient').innerHTML = '';
    heroKey = '';
    closeReport();
    if (state) refresh();
  }

  // ---------------------------------------------------------------- report panel
  function openReport() {
    const n = state.nights.find(x => x.id === selected), r = detail.report || {}, v = r.verdict, p = r.phot || {}, tr = r.transit;
    const facts = [
      ['Frames', r.frames ? r.frames + (r.sampled ? ' of ' + n.frames + ' sampled' : ' × ' + r.exposure + ' s') : n.frames],
      r.cadence_min && !r.sampled ? ['Cadence', r.cadence_min + ' min'] : null,
      tr ? ['Transit', hhmm(tr.ingress) + '–' + hhmm(tr.egress) + ' UT'] : null,
      tr ? ['Mid-transit', tr.mid.slice(11, 19) + ' UT'] : null,
      tr ? ['Expected depth', pct(tr.depth) + ' %'] : null,
      tr && tr.v_mag ? ['Star', 'V ' + tr.v_mag] : null,
      r.telescope ? ['Telescope', r.telescope] : null,
      p.x ? ['Target pixel', p.x + ', ' + p.y] : null,
      p.comps ? ['Comparison stars', p.comps.length] : null,
      p.plate_matched ? ['Field match', p.plate_matched + ' stars, ' + p.plate_rms + ' px'] : null,
      p.peak ? ['Peak', p.peak + ' ADU'] : null,
      p.drift ? ['Drift', (p.drift[1] - p.drift[0]) + ' × ' + (p.drift[3] - p.drift[2]) + ' px'] : null,
      r.fit ? ['Fitted mid-transit', r.fit.tc_bjd.toFixed(4) + ' ± ' + r.fit.tc_err.toFixed(4) + ' BJD'] : null,
      r.fit && r.fit.exotic ? ['EXOTIC', r.fit.exotic] : null,
      r.fit ? ['Frames fitted', r.fit.frames + (r.fit.dropped ? ', ' + r.fit.dropped + ' left out' : '')] : null,
      r.fit && r.fit.files.folder ? ['Upload files in', r.fit.files.folder] : null,
    ].filter(Boolean);
    $('sheet').innerHTML = '<div class="panel" role="dialog" aria-modal="true" aria-label="Report">' +
      '<div class="head"><div><span class="label">Report · ' + day(n.first_frame, true) + '</span><h2>' + esc(planet(n)) + '</h2></div>' +
      '<button class="nav" id="close-report" aria-label="Close">✕</button></div>' +
      '<p class="say">' + sentence(n, r) + '</p><div class="chart" id="report-chart"></div>' +
      (shareable(r) ? '<button class="pill" data-act="share"><span class="arrow">Copy image to share</span><span class="ok">Copied</span></button>' : '') +
      '<div class="checks">' + v.checks.map(c => '<div class="check ' + esc(c.state) + '"><i></i><span>' +
        esc(c.label) + '</span><span class="val num">' + esc(c.value) + '</span></div>').join('') + '</div>' +
      '<div class="facts">' + facts.map(x => '<div><div class="label">' + esc(x[0]) + '</div><div class="v num">' + esc(x[1]) + '</div></div>').join('') + '</div></div>';
    $('sheet').style.setProperty('--tone', rgb(tone(tr, gradeOf(n))));
    $('sheet').hidden = false;
    draw($('report-chart'), n, r, !calm);
    requestAnimationFrame(() => requestAnimationFrame(() => $('sheet').classList.add('in')));
    $('close-report').focus();
  }
  // ---------------------------------------------------------------- night log: every night and why
  let logText = '';
  async function openLog() {
    let log;
    try { log = await api.log(); } catch (e) { return; }
    logText = log.text;
    const counts = ['good', 'improvable', 'lost'].map(g => '<span class="' + g + '-t">' + log.counts[g] + ' ' + g + '</span>').join(' · ') + ' · ' + log.waiting + ' waiting';
    $('sheet').innerHTML = '<div class="panel" role="dialog" aria-modal="true" aria-label="Night log">' +
      '<div class="head"><div><span class="label">Night log</span><h2>All ' + state.nights.length + ' nights</h2></div>' +
      '<button class="nav" id="close-report" aria-label="Close">✕</button></div>' +
      '<p class="say">' + counts + '</p>' +
      (log.causes.length ? '<div><div class="label">Lost to</div><p class="say">' + log.causes.map(c => esc(c[0]) + ' <span class="num">' + c[1] + '</span>').join(' · ') + '</p></div>' : '') +
      (log.notes.length ? '<div><div class="label">Worth reporting</div><ul class="notes">' + log.notes.map(t => '<li>' + esc(t) + '</li>').join('') + '</ul></div>' : '') +
      '<div class="rows">' + state.nights.map(n => '<button class="row" data-night="' + esc(n.id) + '"><span class="d num">' + day(n.first_frame) + '</span><span>' +
        esc(planet(n)) + '</span><span class="r ' + gradeOf(n) + '-t">' + esc(n.headline || 'Waiting') + '</span></button>').join('') + '</div>' +
      '<button class="go" id="copy-log">Copy as text<span>→</span></button></div>';
    $('sheet').hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => $('sheet').classList.add('in')));
    $('close-report').focus();
  }
  function copyLog(button) {
    const done = () => { button.firstChild.textContent = 'Copied'; };
    try { navigator.clipboard.writeText(logText).then(done, () => {}); } catch (e) { /* clipboard unavailable */ }
  }

  // ---------------------------------------------------------------- share: the night as one picture
  function card(n, r) {
    const W = 1600, H = 900, m = 76, tr = r.transit, fit = r.fit, c = tone(tr, gradeOf(n));
    const canvas = document.createElement('canvas'), g = canvas.getContext('2d');
    const star = '#f4ecdd', dim = 'rgba(244,236,221,.62)', faint = 'rgba(244,236,221,.4)', rule = 'rgba(244,236,221,.13)';
    canvas.width = W;
    canvas.height = H;
    g.fillStyle = '#050608';
    g.fillRect(0, 0, W, H);
    const write = (text, x, y, size, o) => {            // o: weight, mono, colour, align, track (letter spacing, in em)
      g.font = (o.weight || 400) + ' ' + size + 'px ' + (o.mono ? '"IBM Plex Mono", monospace' : 'Jost, sans-serif');
      g.fillStyle = o.colour || star;
      const gap = (o.track || 0) * size, parts = gap ? Array.from(text) : [text];
      const width = parts.reduce((sum, ch) => sum + g.measureText(ch).width + gap, -gap);
      let at = o.align === 'right' ? x - width : o.align === 'center' ? x - width / 2 : x;
      parts.forEach(ch => { g.fillText(ch, at, y); at += g.measureText(ch).width + gap; });
      return width;
    };
    const line = (x1, y1, x2, y2, colour, width) => {
      g.strokeStyle = colour;
      g.lineWidth = width || 1;
      g.beginPath();
      g.moveTo(x1, y1);
      g.lineTo(x2, y2);
      g.stroke();
    };

    // who and when, with the star and its planet at mid-transit, to scale
    write(day(n.first_frame, true).toUpperCase() + '  ·  MICROOBSERVATORY', m, m + 14, 15, {weight: 500, colour: dim, track: 0.2});
    write(planet(n), m - 4, m + 112, 96, {weight: 300});
    const side = 300, R = 74, orb = document.createElement('canvas'), art = orb.getContext('2d');
    const k = tr.rp_rs || Math.sqrt(tr.depth), ox = W - m - side / 2 + 40, oy = m + 62;
    orb.width = orb.height = side;
    paintHalo(art, c, R, 1);
    paintStar(art, c, R, n.id);
    g.drawImage(orb, ox - side / 2, oy - side / 2);
    g.fillStyle = '#020203';
    g.beginPath();
    g.arc(ox, oy - Math.min(0.95, tr.impact || 0) * R, Math.max(3, k * R), 0, TURN);
    g.fill();

    // the light curve
    const p = pointsOf(r), model = modelOf(r), left = m, right = W - m, top = 262, floor = 640;
    const t0 = ms(n.first_frame) - 6e5, t1 = ms(n.last_frame) + 6e5, x = t => left + (t - t0) / (t1 - t0) * (right - left);
    const vals = p.map(e => e[1]).sort((u, v) => u - v), q = j => vals[Math.floor(j * (vals.length - 1))];
    const lo = Math.min(1 - tr.depth * 1.9, q(0.03)), hi = Math.max(1 + tr.depth, q(0.97));
    const y = v => top + 10 + (hi - Math.min(hi, Math.max(lo, v))) / (hi - lo) * (floor - top - 20);
    const a = x(ms(tr.ingress)), b = x(ms(tr.egress)), warm = rgb(c);
    g.fillStyle = rgba(c, 0.055);
    g.fillRect(a, top, b - a, floor - top);
    line(a, top - 8, a, floor, rgba(c, 0.55));
    line(b, top - 8, b, floor, rgba(c, 0.55));
    write(hhmm(tr.ingress), a + 8, top - 12, 14, {mono: true, colour: warm});
    write(hhmm(tr.egress), b - 8, top - 12, 14, {mono: true, colour: warm, align: 'right'});
    write('TRANSIT', (a + b) / 2, top - 12, 12, {weight: 500, colour: warm, align: 'center', track: 0.24});
    const step = [0.01, 0.02, 0.05, 0.1].find(v => v * (floor - top - 20) / (hi - lo) >= 34) || 0.2;
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
      const one = Math.abs(v - 1) < 1e-6;
      line(left, y(v), one ? right : left + 7, y(v), one ? rule : faint);
      write(v.toFixed(2), left + 13, y(v) + (one ? -8 : 5), 13, {mono: true, colour: faint});
    }
    g.strokeStyle = warm;
    g.lineWidth = 2.4;
    g.lineJoin = 'round';
    g.beginPath();
    g.moveTo(left, y(1));
    model.points.forEach(e => g.lineTo(x(e[0]), y(e[1])));
    g.lineTo(right, y(1));
    g.stroke();
    const dot = (t, v, r0, colour) => { g.fillStyle = colour; g.beginPath(); g.arc(x(t), y(v), r0, 0, TURN); g.fill(); };
    const bins = {}, mean = (arr, j) => arr.reduce((sum, e) => sum + e[j], 0) / arr.length;
    p.forEach(e => {
      const key = Math.floor((e[0] - t0) / 9e5);
      (bins[key] = bins[key] || []).push(e);
      dot(e[0], e[1], 2.3, faint);
    });
    Object.keys(bins).forEach(key => { if (bins[key].length >= 3) dot(mean(bins[key], 0), mean(bins[key], 1), 5.2, star); });
    line(left, floor, right, floor, rule);
    for (let t = Math.ceil(t0 / 36e5) * 36e5; t < t1; t += 36e5) {
      line(x(t), floor, x(t), floor + 6, faint);
      if (x(t) > left + 30 && x(t) < right - 50) write(String(new Date(t).getUTCHours()).padStart(2, '0') + ':00', x(t), floor + 26, 13, {mono: true, colour: faint, align: 'center'});
    }
    write('UT', right, floor + 26, 13, {mono: true, colour: faint, align: 'right'});

    // the numbers: the fit's when there is one, else the quick look's
    const pm = (v, err, digits) => v.toFixed(digits) + ' ± ' + err.toFixed(digits), oc = fit ? (fit.tc_bjd - tr.tc_bjd) * 1440 : 0;
    const facts = fit ? [['Depth', pm(fit.depth * 100, fit.depth_err * 100, 2), '%'], ['Mid-transit', pm(fit.tc_bjd, fit.tc_err, 4), 'BJD_TDB'],
                         ['Against prediction', (oc >= 0 ? '+' : '−') + pm(Math.abs(oc), fit.tc_err * 1440, 1), 'min'], ['Scatter', pct(fit.scatter, 2), '%']]
      : [['Depth, quick look', pm(r.phot.depth * 100, r.phot.depth_err * 100, 1), '%'], ['Expected', pct(tr.depth, 2), '%'],
         ['Signal', r.phot.snr.toFixed(1), 'σ'], ['Scatter', pct(r.phot.scatter), '%']];
    const widths = facts.map(e => write(e[1], 0, -99, 34, {mono: true, weight: 300}) + 10 + write(e[2], 0, -99, 15, {mono: true}));
    const spare = (right - left - widths.reduce((u, v) => u + v, 0)) / (facts.length - 1);
    let at = left;
    facts.forEach((e, i) => {
      write(e[0].toUpperCase(), at, 716, 12.5, {weight: 500, colour: faint, track: 0.22});
      const w = write(e[1], at, 762, 34, {mono: true, weight: 300});
      write(e[2], at + w + 10, 762, 15, {mono: true, colour: faint});
      at += widths[i] + spare;
    });

    // credit
    line(left, H - m - 22, right, H - m - 22, rule);
    write('Frames: MicroObservatory  ·  ' + (fit ? 'Fit: EXOTIC ' + (fit.exotic || '') : 'Quick look, not a fit') + '  ·  Ephemeris: ' + (tr.source || 'ExoClock'), left, H - m + 8, 15, {colour: dim});
    write(state.name.toUpperCase(), right, H - m + 8, 14, {weight: 500, align: 'right', track: 0.36});
    return canvas;
  }

  const shareable = r => !!(r && r.transit && pointsOf(r) && (r.fit || r.phot.depth != null));

  function share() {                        // onto the clipboard, or saved as a file where the browser will not copy images
    const n = state && state.nights.find(x => x.id === selected);
    if (!n || !detail || detail.id !== n.id || !shareable(detail.report)) return;
    const image = new Promise(done => card(n, detail.report).toBlob(done, 'image/png'));
    const done = () => {                    // the button shows a tick for a moment
      const buttons = document.querySelectorAll('[data-act="share"]');
      buttons.forEach(b => b.classList.add('done'));
      setTimeout(() => buttons.forEach(b => b.classList.remove('done')), 2200);
    };
    const save = () => image.then(blob => {
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = n.id + '.png';
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      done();
    });
    try { navigator.clipboard.write([new ClipboardItem({'image/png': image})]).then(done, save); } catch (e) { save(); }
  }

  // ---------------------------------------------------------------- upload: the good nights, one at a time
  let upload = null;                         // the nights being gone through, the place among them, those found on AAVSO

  function openUpload() {
    if (snap || !state || upload) return;
    closeReport();
    upload = {queue: pending().map(n => n.id), at: 0, found: {}};
    $('upload').hidden = false;
    renderUpload();
    seen(true);
  }

  async function seen(opening) {             // nights that have turned up on AAVSO are marked and shown as found
    const found = upload && await api.uploads().catch(() => ({}));
    if (!upload || !Object.keys(found).length) return;
    await refresh();
    if (!upload) return;
    if (opening) upload.queue = upload.queue.filter(id => !found[id]);           // sent before this round began
    else Object.assign(upload.found, found);
    renderUpload();
  }

  function closeUpload() {
    upload = null;
    $('upload').hidden = true;
    refresh();
  }

  function retitle() {                       // readable from another tab: the night being uploaded, or how far the work is
    const total = state.nights.length, done = state.nights.filter(n => n.stage === 'done').length;
    const n = upload && state.nights.find(x => x.id === upload.queue[upload.at]);
    document.title = n ? planet(n) + ' · ' + (upload.at + 1) + ' of ' + upload.queue.length + ' · ' + state.name
      : state.name + (complete() ? ' · done' : total ? ' · ' + done + '/' + total : '');
  }

  async function renderUpload() {
    const box = $('upload'), id = upload.queue[upload.at], total = upload.queue.length;
    retitle();
    const sent = upload.queue.filter(x => state.nights.find(n => n.id === x).uploaded).length;
    const top = '<header class="u-top"><span class="label">Upload to AAVSO</span>' +
      (total ? '<span class="u-nav"><button class="nav" data-act="back" aria-label="Previous night"' + (upload.at ? '' : ' disabled') + '>‹</button>' +
        '<span class="u-count num">' + (id ? (upload.at + 1) + ' of ' + total : 'Done') + '</span>' +
        '<button class="nav" data-act="next" aria-label="Next night"' + (id ? '' : ' disabled') + '>›</button></span>' : '') +
      '<button class="nav" data-act="close-upload" aria-label="Close">✕</button></header>';
    if (!id) {                               // past the last night
      const left = total - sent;
      const listing = 'https://apps.aavso.org/exosite/?obscode=' + encodeURIComponent(state.observer || '') + '&search=true';
      box.innerHTML = top + '<div class="u-end"><h1 class="name">' + (total ? sent + ' uploaded' : 'Nothing to upload') + '</h1>' +
        '<p class="say">' + (left ? left + ' still to do.' : total ? 'Every good night is in.' : 'Every good night is marked as uploaded.') + '</p>' +
        (state.observer ? '<a class="pill" href="' + esc(listing) + '" target="_blank" rel="noopener noreferrer">See them on AAVSO</a>' : '') + '</div>';
      return;
    }
    let d;
    try { d = await api.night(id); } catch (e) { return; }
    if (!upload || upload.queue[upload.at] !== id) return;      // moved on while it loaded
    const n = state.nights.find(x => x.id === id), r = d.report, files = r.fit.files, last = path => path.split(/[\\/]/).pop();
    const qc = (r.fit.qc || '').split(', KTMF ');
    const notes = r.verdict.checks.filter(c => c.state !== 'ok').map(c => esc(c.label) + ': ' + esc(c.value));
    box.innerHTML = top + '<div class="u-body"><section class="u-night">' +
      '<div><span class="label">' + day(n.first_frame, true) + '</span><h1 class="name">' + esc(planet(n)) + '</h1><p class="say">' + sentence(n, r) + '</p></div>' +
      '<div class="chart" id="upload-chart"></div>' +
      '<div class="stats">' + stats(n, r).slice(0, 3).concat(qc[0] ? [['EXOTIC', qc[0], qc[1] || '']] : []).map(stat).join('') + '</div>' +
      (notes.length ? '<p class="u-notes">' + notes.join(' · ') + '</p>' : '') + '</section>' +
      '<ol class="u-steps' + (n.uploaded ? ' sent' : '') + '">' +
      '<li><span class="num">1</span><div><h3>Find the two files</h3><p class="num">' + [files.report, files.plot].filter(Boolean).map(path => esc(last(path))).join('<br>') + '</p>' +
        '<button class="pill" data-act="reveal">Show the files</button></div></li>' +
      '<li><span class="num">2</span><div><h3>Fill in the AAVSO form</h3><p>Site and equipment: MicroObservatory.<br>Report form: the .txt file. Image(s): the .png.<br>Tick the GDPR box and press Submit.<br>If AAVSO answers "verification failed", reload the form and submit again.</p>' +
        '<button class="pill" data-act="form">Open the form</button></div></li>' +
      '<li><span class="num">' + (n.uploaded ? '✓' : '3') + '</span><div>' + (n.uploaded
        ? '<h3>' + (upload.found[id] ? 'Found on AAVSO' : 'Uploaded') + '</h3><p>' + (upload.found[id] ? 'It is on your list as number ' + upload.found[id] + '.' : day(n.uploaded, true)) + '</p>' +
          '<div class="u-row"><button class="pill solid" data-act="next">' + (upload.at < total - 1 ? 'Next night' : 'Finish') + '</button>' +
          '<button class="go small" data-act="pending">Not uploaded after all</button></div>'
        : '<h3>Come back here</h3><p>' + (state.observer ? 'Umbra looks for the night on AAVSO and tells you when it is there.' : 'Mark the night once it is sent.') + '</p>' +
          '<button class="pill" data-act="uploaded">Mark as uploaded</button>') + '</div></li></ol></div>';
    box.style.setProperty('--tone', rgb(tone(r.transit, 'good')));
    box.scrollTop = 0;
    draw($('upload-chart'), n, r, false);
  }

  async function uploadStep(what) {         // what the buttons of the upload screen do
    const id = upload.queue[upload.at], n = state.nights.find(x => x.id === id);
    if (what === 'reveal') return api.act('reveal', id).catch(() => false);
    if (what === 'form') {                   // opened by this computer; from another device, as a plain link
      if (!await api.form().catch(() => false)) window.open(state.form, '_blank', 'noopener');
      return;
    }
    if (what === 'uploaded' || what === 'pending') {
      if (!await api.act(what, id).catch(() => false)) return;
      n.uploaded = what === 'uploaded' ? new Date().toISOString() : null;
      delete upload.found[id];
    }
    upload.at = Math.min(upload.queue.length, Math.max(0, upload.at + (what === 'back' ? -1 : what === 'next' ? 1 : 0)));
    renderUpload();
  }

  function closeReport() {
    if ($('sheet').hidden) return;
    $('sheet').classList.remove('in');
    setTimeout(() => { $('sheet').hidden = true; $('sheet').innerHTML = ''; }, calm ? 0 : 420);
  }

  // ---------------------------------------------------------------- data flow
  function pickDefault() {
    const fromHash = location.hash.slice(1);
    if (ID.test(fromHash) && state.nights.some(n => n.id === fromHash)) return fromHash;
    for (const g of ['good', 'improvable', 'lost']) {
      const m = state.nights.filter(n => n.grade === g);
      if (m.length) return m[m.length - 1].id;
    }
    return state.nights.length ? state.nights[state.nights.length - 1].id : null;
  }

  let loading = false, again = false;
  async function refresh() {
    if (loading) { again = true; return; }  // a slow answer must not overlap the next one; it is asked again after
    loading = true;
    try { await load(); } finally { loading = false; }
    if (again) { again = false; return refresh(); }
  }

  function keyOf(n) {                       // everything the big view depends on; it is drawn again when this changes
    const view = minimal ? $('ambient') : $('hero'), live = state.activity && state.activity.night;
    return n ? [n.id, n.stage, n.grade, n.uploaded, view.clientWidth, view.clientHeight, live && live !== n.id ? 1 : 0, minimal].join() : '';
  }

  function resized() {                      // drawn again at once from what is loaded; the replay keeps its place
    const n = state && state.nights.find(x => x.id === selected), began = scene && scene.start;
    if (!n || !detail || detail.id !== n.id || keyOf(n) === heroKey) return;
    heroKey = keyOf(n);
    if (minimal) renderAmbient(false);
    else renderHero(false);
    if (scene && began != null) {
      scene.start = began;
      frame(performance.now());             // no frame is shown with the replay back at its start
    }
  }

  async function load() {
    state = await api.state();
    const live = state.activity && state.activity.night;
    if (follow && !minimal && live && state.nights.some(n => n.id === live)) selected = live;
    if (!selected || !state.nights.some(n => n.id === selected)) selected = pickDefault();
    renderBar();
    renderStrip();
    const n = state.nights.find(x => x.id === selected), key = keyOf(n);
    if (key !== heroKey) {
      const fresh = !heroKey || heroKey.split(',')[0] !== (n && n.id);     // animate on a new night, not on a resize
      detail = n ? await api.night(n.id) : null;
      heroKey = key;
      $('tools').querySelector('[data-act="share"]').hidden = !shareable(detail && detail.report);
      if (minimal) renderAmbient(fresh);
      else renderHero(fresh);
    } else if (minimal) {
      ambientStatus();
    } else if (n && n.id === live && $('say')) {
      $('say').innerHTML = sentence(n, detail.report || {});     // download progress on the live night
    }
  }

  function select(id) {
    if (!id || id === selected) return;
    follow = false;
    selected = id;
    try { history.replaceState(null, '', '#' + id); } catch (e) { /* sandboxed frames may refuse */ }
    closeReport();
    refresh();
  }
  const stepBy = d => { const i = state.nights.findIndex(n => n.id === selected) + d; if (state.nights[i]) select(state.nights[i].id); };

  document.addEventListener('click', e => {
    const bar = e.target.closest('.n'), nav = e.target.closest('.nav[data-step]');
    if (bar) return select(bar.dataset.id);
    if (nav) return stepBy(+nav.dataset.step);
    if (e.target.closest('#go-live')) { follow = true; closeReport(); return refresh(); }
    if (e.target.closest('#open-report')) return openReport();
    if (e.target.closest('[data-act="share"]')) return share();
    if (e.target.closest('[data-act="upload"]')) return openUpload();
    if (e.target.closest('[data-act="close-upload"]')) return closeUpload();
    const step = upload && e.target.closest('#upload [data-act]');
    if (step) return uploadStep(step.dataset.act);
    if (e.target.closest('#open-log')) return openLog();
    if (e.target.closest('[data-act="full"]')) return toggleFull();
    if (e.target.closest('[data-act="minimal"]')) return setMinimal(!minimal);
    if (e.target.closest('#copy-log')) return copyLog(e.target.closest('#copy-log'));
    const row = e.target.closest('.row[data-night]');
    if (row) return select(row.dataset.night);
    if (e.target.closest('#close-report') || e.target === $('sheet')) return closeReport();
  });
  // full screen: the standard call, or Safari's older name for it
  const root = document.documentElement, enterFull = root.requestFullscreen || root.webkitRequestFullscreen;
  function toggleFull() {
    const full = document.fullscreenElement || document.webkitFullscreenElement;
    const done = full ? (document.exitFullscreen || document.webkitExitFullscreen).call(document) : enterFull.call(root);
    if (done && done.catch) done.catch(() => {});
  }
  document.querySelector('[data-act="full"]').hidden = !enterFull;

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') return upload ? closeUpload() : closeReport();
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (upload) {                            // in the upload screen only the arrows do anything
      if (e.key === 'ArrowLeft' && upload.at) uploadStep('back');
      else if (e.key === 'ArrowRight' && upload.queue[upload.at]) uploadStep('next');
    }
    else if (e.key === 'f' && enterFull) toggleFull();
    else if (e.key === 'm') setMinimal(!minimal);
    else if (e.key === 'c') share();
    else if (e.key === 'u') openUpload();
    else if (e.key === 'ArrowLeft' && state) stepBy(-1);
    else if (e.key === 'ArrowRight' && state) stepBy(1);
  });
  const returned = () => { if (upload && !document.hidden) seen(); };       // back from AAVSO's form
  document.addEventListener('visibilitychange', returned);
  window.addEventListener('focus', returned);
  let resizing;
  window.addEventListener('resize', () => { cancelAnimationFrame(resizing); resizing = requestAnimationFrame(resized); });

  const offline = () => { $('status').textContent = 'Not connected'; };
  setMinimal(minimal);
  const first = refresh().catch(offline);
  if (!snap) setInterval(() => refresh().catch(offline), 4000);

  // the opening plays once per browser session; after that it only covers the page until the first data is in
  const splash = $('splash');
  if (splash) {
    let seen = false;
    try { seen = !!sessionStorage.getItem('opened'); sessionStorage.setItem('opened', '1'); } catch (e) { /* storage unavailable */ }
    splash.classList.toggle('quick', seen);
    const open = () => splash.classList.add('gone');
    Promise.all([first, new Promise(done => setTimeout(done, seen ? 0 : calm ? 1200 : 2900))]).then(open);
    splash.addEventListener('click', open);
    document.addEventListener('keydown', open, {once: true});
  }
})();
