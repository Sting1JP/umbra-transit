"""Settings and fixed facts. Everything tunable lives here."""
import os
from dataclasses import dataclass, field
from pathlib import Path

# The app's name lives here and nowhere else in the code. To rename the project, see README.
NAME = 'Umbra'             # shown on the screen
SLUG = 'umbra'             # data folder, database file, folder marker, environment variable prefix
HOME_URL = 'https://github.com/Sting1JP/umbra-transit'     # sent with every request, so a server's operator can find the author

LISTING_URL = ('https://waps.cfa.harvard.edu/microobservatory/MOImageDirectory/ImageDirectory.php'
               '?SortBy=&SortPos=&SearchFor=&Type=Object&SortRange=%d')     # everything, for the last 10, 20 or 30 days
IMAGE_URL = 'https://mo-www.cfa.harvard.edu/ImageDirectory/%s.FITS'
EXOCLOCK_URL = 'https://www.exoclock.space/database/planets_json'
ARCHIVE_URL = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync'          # NASA Exoplanet Archive
MAST_URL = 'https://mast.stsci.edu/api/v0/invoke'
FORM_URL = 'https://apps.aavso.org/exosite/submit'      # where a person uploads a night
# an observer's public list of uploads, from a date on; read only
UPLOADS_URL = 'https://apps.aavso.org/exosite/?search=true&format=json&obscode=%s&start_date=%s'
# The only hosts this program ever contacts.
ALLOWED_HOSTS = frozenset({'waps.cfa.harvard.edu', 'mo-www.cfa.harvard.edu', 'www.exoclock.space', 'mast.stsci.edu',
                           'exoplanetarchive.ipac.caltech.edu', 'apps.aavso.org'})

FRAME_BYTES = 656640            # one 650 x 500 16-bit MicroObservatory frame
KEEP_DAYS = 30                  # the directory lists the last 30 days
RECENT_DAYS = 10                # the shorter listing, enough while the last read is newer than that
# Directory target (letters and digits, lower case) -> ExoClock name, where they differ.
ALIASES = {'hatp10': 'WASP-11b'}


def default_data_dir():
    home = Path.home() / 'Documents' / 'Astro' / 'data' / 'exoplanetwatch' / SLUG
    return Path(os.environ.get(SLUG.upper() + '_DATA', home))


@dataclass
class Settings:
    data: Path = field(default_factory=default_data_dir)
    host: str = '127.0.0.1'     # localhost only unless --lan is given
    port: int = 8765
    min_run: int = 20           # this many frames on one target in a night is a transit run
    sample_frames: int = 8      # frames looked at before a full download
    sample_pass: float = 0.6    # share of sampled frames that must be clear
    min_stars: int = 40         # fewer stars than this in the best frames means overcast
    fetch_pause: float = 1.0    # seconds between downloads
    refresh_hours: float = 6    # how often the directory is read again
    reject_keep_days: float = 2 # raw frames of lost nights are deleted after this
    max_full: int = 0           # full downloads per run (0 = no limit)
    observer: str = ''          # AAVSO observer code written into EXOTIC reports
    reduce_timeout: int = 2700  # seconds EXOTIC may take for one night
    min_free_gb: float = 2      # downloads wait while the disk has less room than this

    @property
    def nights_dir(self):
        return self.data / 'nights'

    @property
    def db_path(self):
        return self.data / (SLUG + '.sqlite')
