"""The local screen: a small HTTP server that reads state and takes a few actions from this computer.

Safety: binds to localhost unless --lan is given; rejects requests whose Host header is not this
machine (blocks DNS rebinding); serves a fixed list of files (no path handling); sends a strict
Content-Security-Policy; every action needs a custom header and a local client.
"""
import base64
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, digest, share
from .config import FORM_URL, KEEP_DAYS, NAME, SLUG
from .directory import ID_PATTERN
from .grade import GOOD

WEB = Path(__file__).parent / 'web'
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.css': ('app.css', 'text/css; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/icon.svg': ('icon.svg', 'image/svg+xml'),
          '/icon.png': ('icon.png', 'image/png'), '/favicon.ico': ('icon.png', 'image/png')}
FONTS = ('jost.woff2', 'plex-mono-300.woff2', 'plex-mono-400.woff2')
STATIC.update({'/fonts/' + name: ('fonts/' + name, 'font/woff2') for name in FONTS})
_NIGHT = re.compile('^/api/night/(%s)$' % ID_PATTERN)
_ACTION = re.compile('^/api/(reveal|uploaded|pending)/(%s)$' % ID_PATTERN)
_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; font-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
_SUMMARY = ('id', 'target', 'planet', 'first_frame', 'last_frame', 'frames', 'stage', 'grade', 'headline', 'uploaded')


def summary(night, now=None):
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(night['first_frame'])).total_seconds() / 86400
    return dict({k: night[k] for k in _SUMMARY}, days_left=round(KEEP_DAYS - age, 1))


_rated = {}     # night id -> (when the night last changed, its share rating): the full report is read once per change


def ratings(pipeline, rows):
    """The share rating of every night that has one, by id, with the three best marked."""
    found = {}
    for row in rows:
        if row['grade'] != GOOD:
            continue
        if _rated.get(row['id'], (None,))[0] != row['updated']:
            _rated[row['id']] = (row['updated'], share.rate(row['grade'], pipeline.store.get(row['id'])['report'] or {}))
        if _rated[row['id']][1]:
            found[row['id']] = dict(_rated[row['id']][1])
    for nid in sorted(found, key=lambda i: -found[i]['score'])[:3]:
        found[nid]['top'] = True
    return found


def state(pipeline):
    again = pipeline.refreshed + pipeline.settings.refresh_hours * 3600 - time.time()      # until the directory is read again
    rows = pipeline.store.all()
    rated = ratings(pipeline, rows)
    return dict(name=NAME, version=__version__, activity=pipeline.activity, hold=pipeline.hold, observer=pipeline.settings.observer, form=FORM_URL,
                next_look=max(0, round(again)) if pipeline.refreshed else None,
                nights=[dict(summary(n), share=rated.get(n['id'])) for n in rows])


def log(pipeline):
    nights = pipeline.store.all(full=True)
    return dict(digest.build(nights), text=digest.markdown(nights, NAME))


def night(pipeline, night_id):
    row = pipeline.store.get(night_id)
    return dict(summary(row), report=row['report']) if row else None


def reveal(pipeline, night_id):
    """Open a folder holding only the files to upload: the AAVSO report and its plot. True if they were there."""
    row = pipeline.store.get(night_id)
    files = (((row or {}).get('report') or {}).get('fit') or {}).get('files', {})
    nights = pipeline.settings.nights_dir.resolve()
    report, plot = (Path(files.get(key) or '') for key in ('report', 'plot'))
    own = [path for path in (report, plot) if path.is_file() and nights in path.resolve().parents]
    if report not in own:
        return False
    folder = pipeline.folder(night_id) / 'upload'
    shutil.rmtree(folder, ignore_errors=True)               # always the files as they are now
    folder.mkdir()
    for path in own:
        shutil.copy2(path, folder)
    opener = 'open' if sys.platform == 'darwin' else 'explorer' if os.name == 'nt' else 'xdg-open'
    try:
        subprocess.Popen([opener, str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:                                         # no file manager to ask
        return False
    return True


def open_form():
    """Open AAVSO's form in the browser, as a typed address would.

    Safari refuses a form that was reached from a link on another site, this page included.
    """
    try:
        return webbrowser.open(FORM_URL)
    except webbrowser.Error:
        return False


def act(pipeline, action, night_id):
    """One of the actions the upload screen offers. True if it was done."""
    if action == 'reveal':
        return reveal(pipeline, night_id)
    if not pipeline.store.get(night_id, full=False):
        return False
    pipeline.store.save(night_id, uploaded=datetime.now(timezone.utc).isoformat(timespec='seconds') if action == 'uploaded' else None)
    return True


def _page():
    return (WEB / 'index.html').read_text().replace('{{name}}', NAME)


def _inline_fonts(css):
    """The stylesheet with its font files embedded, for a single-file snapshot."""
    for name in FONTS:
        data = base64.b64encode((WEB / 'fonts' / name).read_bytes()).decode()
        css = css.replace('url(fonts/%s)' % name, 'url(data:font/woff2;base64,%s)' % data)
    return css


def export_snapshot(pipeline, path):
    """Write the screen as one self-contained file with the current data inlined."""
    page = _page()
    body = page.split('<!--app-->')[1].split('<!--/app-->')[0]
    data = dict(state=dict(state(pipeline), activity=None, snapshot=datetime.now(timezone.utc).isoformat()),
                nights={n['id']: night(pipeline, n['id']) for n in pipeline.store.all()}, log=log(pipeline))
    blob = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
    parts = ['<!doctype html>\n<meta charset="utf-8">\n<title>%s</title>\n' % NAME,
             '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n',
             '<style>\n', _inline_fonts((WEB / 'app.css').read_text()), '</style>\n', body,
             '<script>window.APP_SNAPSHOT=', blob, ';</script>\n<script>\n', (WEB / 'app.js').read_text(), '</script>\n']
    Path(path).write_text(''.join(parts))


def _local_host(host, port):
    """True if a Host header names this machine: localhost, an IP address or a .local name."""
    name, _, got = (host or '').rpartition(':')
    if not name or got != str(port):
        return False
    if name == 'localhost' or name.endswith('.local'):
        return True
    try:
        ipaddress.ip_address(name.strip('[]'))
        return True
    except ValueError:
        return False


def make_handler(pipeline, port):
    class Handler(BaseHTTPRequestHandler):
        server_version = SLUG

        def log_message(self, *args):       # keep the terminal quiet
            pass

        def _send(self, code, body, ctype, keep=False):
            self.send_response(code)
            for key, value in (('Content-Type', ctype), ('Content-Length', str(len(body))),
                               ('Content-Security-Policy', _CSP), ('X-Content-Type-Options', 'nosniff'),
                               ('Referrer-Policy', 'no-referrer'), ('Cache-Control', 'max-age=604800' if keep else 'no-store')):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code=200):
            self._send(code, json.dumps(data).encode(), 'application/json')

        def _allowed(self):
            if _local_host(self.headers.get('Host'), port):
                return True
            self._json(dict(error='forbidden'), 403)
            return False

        def do_GET(self):
            if not self._allowed():
                return
            path = self.path.split('?', 1)[0]
            match = _NIGHT.match(path)
            if path in STATIC:
                name, ctype = STATIC[path]
                # the icon may be kept: Safari drops a tab icon it is not allowed to store
                self._send(200, _page().encode() if path == '/' else (WEB / name).read_bytes(), ctype, keep=name.startswith('icon'))
            elif path == '/api/state':
                self._json(state(pipeline))
            elif path == '/api/log':
                self._json(log(pipeline))
            elif match and (data := night(pipeline, match.group(1))):
                self._json(data)
            else:
                self._json(dict(error='not found'), 404)

        def do_POST(self):
            if not self._allowed():
                return
            match = _ACTION.match(self.path)
            if not (ipaddress.ip_address(self.client_address[0]).is_loopback and self.headers.get('X-Local-App') == '1'):
                self._json(dict(error='forbidden'), 403)
            elif self.path == '/api/form':
                self._json(dict(ok=open_form()))
            elif self.path == '/api/uploads':
                found = pipeline.find_uploads()
                self._json(dict(ok=found is not None, found=found or {}))
            elif match:
                done = act(pipeline, *match.groups())
                self._json(dict(ok=done), 200 if done else 404)
            else:
                self._json(dict(error='not found'), 404)

    return Handler


def serve(settings, pipeline, open_browser=True):
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(pipeline, settings.port))
    worker = threading.Thread(target=pipeline.run_forever, daemon=True)
    worker.start()                                          # only once the port is ours
    for name in ('SIGTERM', 'SIGHUP'):                      # a closed terminal or a kill stops as cleanly as Ctrl-C
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.default_int_handler)
    url = 'http://localhost:%d' % settings.port
    print('%s is running at %s  (Ctrl-C to stop)' % (NAME, url))
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.stop()
        server.server_close()
        worker.join(15)                                     # lets a running EXOTIC be stopped
