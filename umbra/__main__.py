"""Command line: `python -m umbra` starts the autopilot and the local screen."""
import argparse
import sys
from datetime import datetime
import webbrowser
from pathlib import Path

from .config import NAME, SLUG, Settings
from .pipeline import Pipeline
from .store import Store


def claim(data, port):
    """Take the data folder for this process, so that two autopilots never work on the same nights.
    Returns the open lock (keep it), or None when another one is running; its port is in the file."""
    try:
        import fcntl
    except ImportError:                 # Windows: no lock
        return True
    lock = open(data / (SLUG + '.lock'), 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    lock.truncate(0)
    lock.write(str(port))
    lock.flush()
    return lock


def main():
    ap = argparse.ArgumentParser(prog=SLUG, description=__doc__)
    ap.add_argument('--data', type=Path, help='data folder (default: the Exoplanet Watch folder)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--lan', action='store_true', help='also serve other devices on this network (read-only screen)')
    ap.add_argument('--no-open', action='store_true', help='do not open the screen in the browser at start')
    ap.add_argument('--observer', help='your AAVSO observer code, written into EXOTIC reports (remembered)')
    ap.add_argument('--once', action='store_true', help='process the directory once and exit, without the screen')
    ap.add_argument('--max-full', type=int, default=0, help='full downloads per run (0 = no limit)')
    ap.add_argument('--log', action='store_true', help='print the night log (every night with its reason) and exit')
    ap.add_argument('--events', action='store_true', help='print what the autopilot did lately, errors included, and exit')
    ap.add_argument('--export', type=Path, help='write a single-file snapshot of the screen and exit')
    a = ap.parse_args()

    settings = Settings(port=a.port, max_full=a.max_full, host='0.0.0.0' if a.lan else '127.0.0.1')
    if a.data:
        settings.data = a.data.expanduser().resolve()
    settings.data.mkdir(parents=True, exist_ok=True)
    remembered = settings.data / 'observer.txt'
    if a.observer:
        remembered.write_text(a.observer.strip())
    settings.observer = remembered.read_text().strip() if remembered.exists() else ''
    store = Store(settings.db_path)
    pipeline = Pipeline(settings, store)

    if a.log:
        from . import digest
        print(digest.markdown(store.all(full=True), NAME))
        return
    if a.events:
        for row in reversed(store.recent(40)):
            print('%s  %-18s %s' % (datetime.fromtimestamp(row['ts']).strftime('%d %b %H:%M'), row['night'] or '', row['message']))
        return
    if a.export:
        from .server import export_snapshot
        export_snapshot(pipeline, a.export)
        return

    lock = claim(settings.data, 0 if a.once else settings.port)
    if lock is None:
        port = (settings.data / (SLUG + '.lock')).read_text().strip()
        if a.once or not port.isdigit() or port == '0':
            sys.exit('%s is already working on this folder.' % NAME)
        url = 'http://localhost:' + port
        print('%s is already running: %s' % (NAME, url))
        if not a.no_open:
            webbrowser.open(url)
        return
    if a.once:
        if pipeline.due():
            pipeline.refresh()
        pipeline.run_once()
        for n in store.all():
            print('%-18s %-8s %-10s %s' % (n['id'], n['stage'], n['grade'] or '-', n['headline'] or ''))
    else:
        from .server import serve
        try:
            serve(settings, pipeline, open_browser=not a.no_open)
        except OSError as exc:
            sys.exit('Port %d cannot be used (%s). Try: python -m %s --port %d' % (settings.port, exc.strerror, SLUG, settings.port + 1))


if __name__ == '__main__':
    main()
