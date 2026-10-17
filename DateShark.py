"""
OriginalDateShark - Multi-Format Original Release Date & Year Tagger
===================================================================
Looks up the ORIGINAL release date of each album in a local audio library on
Discogs, MusicBrainz, and the iTunes/Apple Music Search API, writing it to BOTH:
  1. The "Original Release Date" metadata frames.
  2. The primary "Year / Date" frames (visible in Mp3tag's default grid).

Fixes applied:
  - Eliminates ID3v2.3 multivalued tag duplication ('2007\\2007' in Mp3tag)
    by setting native v2.3 frames (TORY + TYER) and purging TDOR before saving.
  - WAV/AIFF are written through their container classes (no more file corruption).
  - Multi-format support (MP3, FLAC, OGG, OPUS, M4A, MP4, WAV, AIFF).
  - 3-tier lookup cascade: Discogs -> MusicBrainz -> iTunes Search API.

Dependencies:
    pip install requests mutagen
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

# --- DEPENDENCY CHECK ---
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    import mutagen
    from mutagen.id3 import ID3, TDOR, TDRC, TORY, TYER, ID3NoHeaderError
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.mp4 import MP4
    from mutagen.wave import WAVE
    from mutagen.aiff import AIFF
except ImportError as e:
    module_name = (getattr(e, "name", None) or str(e)).split(".")[0]
    print("=" * 60)
    print(f" [!] MISSING REQUIRED MODULE: {module_name}")
    print(f"     Please run: pip install {module_name}")
    sys.exit(1)

if sys.platform == 'win32':
    os.system('')

log = logging.getLogger("origdate")

# ==========================================
# CONFIGURATION & SUPPORTED FORMATS
# ==========================================
__version__ = "2.3.0"
USER_AGENT = f"OriginalDateShark/{__version__} ( https://github.com/example/OriginalDateShark )"

APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CONFIG_KEYS = ("DISCOGS_TOKEN",)

SUPPORTED_EXTENSIONS = (
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".mp4", ".wav", ".aif", ".aiff"
)


def load_config(path=CONFIG_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return {}


def _pick(key, cfg, env, default=""):
    return str(cfg.get(key) or env.get(key) or default)


def ensure_config_template(path=CONFIG_PATH):
    if os.path.exists(path):
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: "" for k in CONFIG_KEYS}, f, indent=2)
        return True
    except OSError as exc:
        log.warning("Could not create %s: %s", path, exc)
        return False


_external_config = load_config()
DISCOGS_TOKEN = _pick("DISCOGS_TOKEN", _external_config, os.environ)

SCHEMA_VERSION = "2.2"
DISCOGS_API = "https://api.discogs.com"
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
ITUNES_API = "https://itunes.apple.com/search"

MIN_MATCH = 0.7
MAX_RELEASE_LOOKUPS = 3
MIN_YEAR = 1860

RATE_LIMITS = {
    "api.discogs.com": 1.0,
    "musicbrainz.org": 1.1,
    "itunes.apple.com": 3.0,  # Apple allows roughly 20 requests/minute
}
DEFAULT_INTERVAL = 0.5

TERMINAL_STATUSES = {"tagged", "matched", "declined"}
TAG_KEYS = ("artist", "albumartist", "album", "compilation", "original_date", "release_date")
VARIOUS_ARTISTS = {"various artists", "various", "va", "v/a", "v.a."}

COMP_FOLDER_RE = re.compile(
    r"various artists|various\s*[-\u2013]|^(?:va|v\.a\.|v/a)(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:va|v\.a\.)\s*[-\u2013]"
)
FEAT_SPLIT_RE = re.compile(
    r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring)\b|\s+(?:feat\.?|ft\.?|featuring)\s+|\s*;\s*",
    re.IGNORECASE,
)
COLLAB_SPLIT_RE = re.compile(r"\s+&\s+|,\s+|\s+/\s+")


# ==========================================
# PURE HELPERS
# ==========================================
def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def is_various(name):
    return bool(name) and name.strip().lower() in VARIOUS_ARTISTS


def artist_candidates(raw_artist):
    base = FEAT_SPLIT_RE.split(raw_artist)[0].strip() or raw_artist.strip()
    first = COLLAB_SPLIT_RE.split(base)[0].strip()
    return [base] + ([first] if first and first != base else [])


def clean_album_title(raw):
    t = re.sub(r'\(.*?\)|\[.*?\]', '', raw).strip()
    t = re.sub(r'[_:\-]', ' ', t)
    t = re.sub(r'[!?]+$', '', t).strip()
    t = re.sub(r'\bvol\.?\s*(\d+)\b', r'Volume \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    if t.lower().startswith("the ") and len(t) > 4:
        t = t[4:].strip()
    return t or raw


def _sim_norm(text):
    t = strip_accents(text).lower()
    stripped = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = stripped if re.search(r"\w", stripped) else t
    t = re.sub(r"[\W_]+", " ", t).strip()
    return t[4:] if t.startswith("the ") else t


def similar(a, b):
    na, nb = _sim_norm(a or ""), _sim_norm(b or "")
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    if min(len(na), len(nb)) >= 6 and (na in nb or nb in na):
        length_ratio = min(len(na), len(nb)) / max(len(na), len(nb))
        if length_ratio > 0.70:
            ratio = max(ratio, 0.9)
    return ratio


def parse_year(value):
    try:
        year = int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None
    return year if MIN_YEAR <= year <= datetime.now().year + 1 else None


_DATE_RE = re.compile(r"^\s*(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")


def normalize_date(text):
    m = _DATE_RE.match(str(text)) if text else None
    if not m:
        return None
    year = parse_year(m.group(1))
    if not year:
        return None
    out = f"{year:04d}"
    month = int(m.group(2)) if m.group(2) else 0
    if 1 <= month <= 12:
        out += f"-{month:02d}"
        day = int(m.group(3)) if m.group(3) else 0
        if 1 <= day <= 31:
            out += f"-{day:02d}"
    return out


def date_matches(existing, target):
    """True if an existing tag value already equals (or is at least as precise as) target."""
    return bool(existing) and (normalize_date(existing) or existing).startswith(target)


def lucene_escape(text):
    """Escape the characters that would break a double-quoted Lucene phrase (MusicBrainz)."""
    return re.sub(r'([\\"])', r'\\\1', text)


def pick_best_date(candidates):
    cands = [d for d in (normalize_date(c) for c in candidates) if d]
    if not cands:
        return None
    precise = [d for d in cands if len(d) > 4] or cands
    earliest = min(precise)
    same = [d for d in precise if d.startswith(earliest)]
    longest = max(len(d) for d in same)
    return min(d for d in same if len(d) == longest)


# ==========================================
# MULTI-FORMAT AUDIO TAG HANDLER
# ==========================================
class AudioFileHandler:
    IFF_EXTS = (".wav", ".aif", ".aiff")

    @staticmethod
    def _id3_major(path):
        """ID3v2 major version to write (3 or 4). Only MP3 can be v2.3; WAV/AIFF keep
        their ID3 data in an embedded chunk and always use v2.4."""
        if not path.lower().endswith(".mp3"):
            return 4
        try:
            with open(path, "rb") as f:
                head = f.read(10)
            return head[3] if head[:3] == b"ID3" and head[3] in (3, 4) else 4
        except OSError:
            return 4

    @classmethod
    def effective_date(cls, path, date):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mp3" and cls._id3_major(path) == 3:
            return date[:4]
        return date

    @classmethod
    def _open_id3(cls, path):
        """Return (tags, major, save). save() writes the tags back safely.

        WAV/AIFF must be saved through WAVE/AIFF: ID3.save(path) would prepend an
        ID3 header to the file and destroy the RIFF/FORM structure."""
        ext = os.path.splitext(path)[1].lower()
        if ext in cls.IFF_EXTS:
            audio = WAVE(path) if ext == ".wav" else AIFF(path)
            if audio.tags is None:
                audio.add_tags()
            return audio.tags, 4, audio.save
        major = cls._id3_major(path)
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        return tags, major, lambda: tags.save(path, v2_version=major)

    @classmethod
    def read_metadata(cls, path):
        try:
            audio = mutagen.File(path)
            if audio is None:
                return None
        except Exception as exc:
            log.debug("Cannot open %s: %s", path, exc)
            return None

        artist = albumartist = album = compilation = orig_date = rel_date = ""

        # 1. MP4 / M4A (iTunes atoms)
        if isinstance(audio, MP4):
            tags = audio.tags or {}
            artist = str(tags.get("\xa9ART", [""])[0])
            albumartist = str(tags.get("aART", [""])[0])
            album = str(tags.get("\xa9alb", [""])[0])
            compilation = "1" if tags.get("cpil", False) else ""
            orig_date = str(tags.get("\xa9day", [""])[0])
            rel_date = orig_date

        # 2. FLAC, Ogg Vorbis, Ogg Opus (Vorbis Comments)
        elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            tags = audio.tags or {}
            def get_vorbis(k):
                vals = tags.get(k, []) or tags.get(k.lower(), [])
                return str(vals[0]).strip() if vals else ""
            artist = get_vorbis("ARTIST")
            albumartist = get_vorbis("ALBUMARTIST") or get_vorbis("ALBUM ARTIST")
            album = get_vorbis("ALBUM")
            compilation = get_vorbis("COMPILATION")
            orig_date = get_vorbis("ORIGINALDATE") or get_vorbis("ORIGINALYEAR")
            rel_date = get_vorbis("DATE")

        # 3. MP3, WAV, AIFF (ID3 frames with safe attribute validation)
        else:
            tags = getattr(audio, "tags", None)
            if tags is None:
                return dict.fromkeys(TAG_KEYS, "")
            def get_id3(k):
                try:
                    if k in tags and hasattr(tags[k], "text") and tags[k].text:
                        return str(tags[k].text[0]).strip()
                    return ""
                except Exception:
                    return ""
            artist = get_id3("TPE1")
            albumartist = get_id3("TPE2")
            album = get_id3("TALB")
            compilation = get_id3("TCMP")
            orig_date = get_id3("TDOR") or get_id3("TORY")
            rel_date = get_id3("TDRC") or get_id3("TYER")

        return {
            "artist": artist.strip(),
            "albumartist": albumartist.strip(),
            "album": album.strip(),
            "compilation": compilation.strip(),
            "original_date": orig_date.strip(),
            "release_date": rel_date.strip(),
        }

    @classmethod
    def write_dates(cls, path, date):
        ext = os.path.splitext(path)[1].lower()
        old_meta = cls.read_metadata(path)
        previous_state = {
            "orig": [old_meta["original_date"]] if old_meta and old_meta["original_date"] else None,
            "date": [old_meta["release_date"]] if old_meta and old_meta["release_date"] else None
        }

        # 1. MP4 / M4A (©day)
        if ext in (".m4a", ".mp4"):
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags["\xa9day"] = [date]
            audio.save()
            return previous_state

        # 2. FLAC / OGG / OPUS (ORIGINALDATE + DATE)
        if ext in (".flac", ".ogg", ".opus"):
            audio = mutagen.File(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags["ORIGINALDATE"] = date
            audio.tags["DATE"] = date
            audio.save()
            return previous_state

        # 3. MP3 / WAV / AIFF (ID3)
        tags, major, save = cls._open_id3(path)
        encoding = 3 if major == 4 else 1
        year_str = date[:4]

        if major == 4:
            # ID3v2.4: Clean TDOR and TDRC timestamps
            tags.delall("TORY")
            tags.delall("TYER")
            tags.setall("TDOR", [TDOR(encoding=encoding, text=[date])])
            tags.setall("TDRC", [TDRC(encoding=encoding, text=[date])])
        else:
            # ID3v2.3: Remove TDOR/TDRC entirely to prevent mutagen from generating
            # duplicate TORY frames during save(v2_version=3) conversion
            tags.delall("TDOR")
            tags.delall("TDRC")
            tags.setall("TORY", [TORY(encoding=encoding, text=[year_str])])
            tags.setall("TYER", [TYER(encoding=encoding, text=[year_str])])

        save()
        return previous_state

    @classmethod
    def restore_dates(cls, path, old, expected, force=False, dry_run=False):
        if not os.path.exists(path):
            return "missing"
        curr = cls.read_metadata(path)
        if curr is None:
            return "missing"
        if not force and curr["original_date"] != expected:
            return "changed"
        if dry_run:
            return "restored"

        ext = os.path.splitext(path)[1].lower()
        old_orig = old.get("orig", [""])[0] if isinstance(old, dict) and old.get("orig") else ""
        old_date = old.get("date", [""])[0] if isinstance(old, dict) and old.get("date") else ""

        if ext in (".m4a", ".mp4"):
            audio = MP4(path)
            if audio.tags:
                if old_orig or old_date:
                    audio.tags["\xa9day"] = [old_orig or old_date]
                else:
                    audio.tags.pop("\xa9day", None)
                audio.save()
        elif ext in (".flac", ".ogg", ".opus"):
            audio = mutagen.File(path)
            if audio.tags:
                # Vorbis comments are case-insensitive and their pop() takes no default
                for key, value in (("ORIGINALDATE", old_orig), ("DATE", old_date)):
                    if value:
                        audio.tags[key] = value
                    elif key in audio.tags:
                        del audio.tags[key]
                audio.save()
        else:
            tags, major, save = cls._open_id3(path)

            tags.delall("TDOR")
            tags.delall("TORY")
            tags.delall("TDRC")
            tags.delall("TYER")

            encoding = 3 if major == 4 else 1
            if major == 4:
                if old_orig:
                    tags.setall("TDOR", [TDOR(encoding=encoding, text=[old_orig])])
                if old_date:
                    tags.setall("TDRC", [TDRC(encoding=encoding, text=[old_date])])
            else:
                if old_orig:
                    tags.setall("TORY", [TORY(encoding=encoding, text=[old_orig[:4]])])
                if old_date:
                    tags.setall("TYER", [TYER(encoding=encoding, text=[old_date[:4]])])

            save()

        return "restored"


def timed_prompt(prompt_text, timeout=5):
    sys.stdout.write(prompt_text)
    sys.stdout.flush()

    if sys.platform == "win32":
        import msvcrt
        start_time = time.time()
        ans = ""
        while time.time() - start_time < timeout:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ('\r', '\n'):
                    print()
                    return ans.lower().strip()
                if char == '\x03':  # getwch() swallows Ctrl+C; re-raise it
                    print()
                    raise KeyboardInterrupt
                if char in ('\x00', '\xe0'):
                    msvcrt.getwch()
                elif char == '\x08':
                    if ans:
                        ans = ans[:-1]
                        sys.stdout.write('\b \b')
                        sys.stdout.flush()
                else:
                    ans += char
                    sys.stdout.write(char)
                    sys.stdout.flush()
            time.sleep(0.05)
        print()
        return ""
    else:
        import select
        i, _, _ = select.select([sys.stdin], [], [], timeout)
        if i:
            return sys.stdin.readline().strip().lower()
        print()
        return ""


class HostThrottle:
    def __init__(self, intervals, default=DEFAULT_INTERVAL):
        self.intervals, self.default = intervals, default
        self.next_ok = {}
        self.lock = threading.Lock()

    def wait(self, host):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_ok.get(host, 0.0))
            self.next_ok[host] = slot + self.intervals.get(host, self.default)
        if slot > now:
            time.sleep(slot - now)


# ==========================================
# BASE LOOKUP FINDER (Fixed 429 recursion)
# ==========================================
class BaseFinder:
    def __init__(self, throttle):
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.throttle = throttle
        self.last_error = None

    def close(self):
        self.session.close()

    def _fetch_json(self, url, params=None, headers=None):
        host = urllib.parse.urlsplit(url).netloc
        hdrs = {"User-Agent": USER_AGENT}
        if headers:
            hdrs.update(headers)
            
        max_retries = 3
        for attempt in range(max_retries + 1):
            self.throttle.wait(host)
            try:
                resp = self.session.get(url, params=params, headers=hdrs, timeout=12)
            except requests.RequestException as exc:
                self.last_error = type(exc).__name__
                log.debug("%s -> %s", url, exc)
                return None

            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                if attempt == max_retries:
                    self.last_error = "HTTP 429 (Max retries exceeded)"
                    return None
                try:
                    retry_after = min(int(float(resp.headers.get("Retry-After", 5))), 60)
                except ValueError:  # Retry-After may be an HTTP-date
                    retry_after = 5
                log.warning("Rate limit hit on %s. Sleeping %ds (attempt %d/%d)...", host, retry_after, attempt + 1, max_retries)
                time.sleep(retry_after)
                continue
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code}"
                log.debug("%s -> HTTP %s", url, resp.status_code)
                return None
            try:
                return resp.json()
            except ValueError:
                self.last_error = "bad JSON"
                return None
        return None


# ==========================================
# DISCOGS LOOKUP
# ==========================================
class DiscogsDateFinder(BaseFinder):
    def __init__(self, throttle, year_only=False, master_only=False):
        super().__init__(throttle)
        self.year_only = year_only
        self.master_only = master_only

    def _fetch(self, url, params=None):
        headers = {"Authorization": f"Discogs token={DISCOGS_TOKEN}"} if DISCOGS_TOKEN else {}
        return self._fetch_json(url, params=params, headers=headers)

    @staticmethod
    def _score(title, artist, album):
        artist_part, sep, album_part = title.partition(" - ")
        if not sep:
            artist_part, album_part = "", title
        artist_part = re.sub(r"\s*\(\d+\)$", "", artist_part)
        check_artist = bool(artist) and not is_various(artist) and bool(artist_part)
        a = similar(artist_part, artist) if check_artist else 1.0
        b = similar(album_part, album)
        if a < MIN_MATCH or b < MIN_MATCH:
            return None
        return a + b

    def _search(self, kind, artist, album):
        params = {"type": kind, "per_page": 10, "release_title": album}
        if artist and not is_various(artist):
            params["artist"] = artist
        data = self._fetch(f"{DISCOGS_API}/database/search", params=params)
        scored = []
        for r in (data or {}).get("results") or []:
            s = self._score(r.get("title", ""), artist, album)
            if s is not None:
                scored.append((s, r))
        scored.sort(key=lambda x: (-round(x[0], 2), parse_year(x[1].get("year")) or 9999))
        return scored

    def _full_date(self, master_id, year):
        data = self._fetch(f"{DISCOGS_API}/masters/{master_id}/versions",
                           params={"per_page": 50, "sort": "released", "sort_order": "asc"})
        versions = [v for v in (data or {}).get("versions") or []
                    if (normalize_date(v.get("released")) or "").startswith(str(year))]
        best = pick_best_date([v.get("released") for v in versions])
        if best and len(best) > 4:
            return best
        found = [best] if best else []
        for v in versions[:MAX_RELEASE_LOOKUPS]:
            if not v.get("id"):
                continue
            rel = self._fetch(f"{DISCOGS_API}/releases/{v['id']}")
            d = normalize_date((rel or {}).get("released"))
            if d and d.startswith(str(year)):
                found.append(d)
        return pick_best_date(found) or str(year)

    def _resolve_master(self, master_id, year_hint):
        year = parse_year(year_hint)
        if not year:
            year = parse_year(((self._fetch(f"{DISCOGS_API}/masters/{master_id}")) or {}).get("year"))
        if not year:
            return None, ""
        if self.year_only:
            return str(year), ""
        date = self._full_date(master_id, year)
        return date, ("" if len(date) > 4 else " (year only)")

    def find_date(self, artist, album):
        self.last_error = None
        if not DISCOGS_TOKEN:
            return None, "DISCOGS_TOKEN not set"
        if not album:
            return None, "no album title"

        masters = self._search("master", artist, album)
        if masters:
            r = masters[0][1]
            date, note = self._resolve_master(r.get("id"), r.get("year"))
            if date:
                return date, f"Discogs master {r['id']}{note}"

        if self.master_only:
            return None, self._failure("No Discogs master found")

        releases = self._search("release", artist, album)
        if not releases:
            return None, self._failure("No Discogs match")
        top = releases[0][0]
        close = [r for s, r in releases if s >= top - 0.1]

        master_id = next((r.get("master_id") for r in close if r.get("master_id")), 0)
        if master_id:
            date, note = self._resolve_master(master_id, None)
            if date:
                return date, f"Discogs master {master_id} (via release search){note}"

        dated = [(parse_year(r.get("year")), r) for r in close]
        dated = [(y, r) for y, r in dated if y]
        if not dated:
            return None, self._failure("Matches had no release year")
        year, rel = min(dated, key=lambda x: x[0])
        date = str(year)
        if not self.year_only and rel.get("id"):
            d = normalize_date(((self._fetch(f"{DISCOGS_API}/releases/{rel['id']}")) or {}).get("released"))
            if d and d.startswith(str(year)):
                date = d
        return date, f"Discogs release {rel.get('id')} (no master)"

    def _failure(self, text):
        return f"{text} (ERROR: {self.last_error})" if self.last_error else text


# ==========================================
# MUSICBRAINZ LOOKUP (BACKUP 1)
# ==========================================
class MusicBrainzDateFinder(BaseFinder):
    def __init__(self, throttle, year_only=False):
        super().__init__(throttle)
        self.year_only = year_only

    def _fetch(self, endpoint, params=None):
        p = {"fmt": "json"}
        if params:
            p.update(params)
        return self._fetch_json(f"{MUSICBRAINZ_API}/{endpoint}", params=p)

    def _full_date_from_releases(self, release_group_id, year):
        data = self._fetch("release", params={"release-group": release_group_id, "limit": 25})
        releases = (data or {}).get("releases") or []
        dates = []
        for r in releases:
            d = normalize_date(r.get("date"))
            if d and d.startswith(str(year)):
                dates.append(d)
        return pick_best_date(dates) or str(year)

    def find_date(self, artist, album):
        self.last_error = None
        if not album:
            return None, "no album title"

        query_parts = [f'releasegroup:"{lucene_escape(album)}"']
        if artist and not is_various(artist):
            query_parts.append(f'artist:"{lucene_escape(artist)}"')
        query = " AND ".join(query_parts)

        data = self._fetch("release-group", params={"query": query, "limit": 10})
        rgroups = (data or {}).get("release-groups") or []
        scored = []
        for rg in rgroups:
            title = rg.get("title", "")
            artist_credit = "".join((ac.get("name") or "") + (ac.get("joinphrase") or "")
                                    for ac in rg.get("artist-credit", []))
            check_artist = bool(artist) and not is_various(artist)
            a = similar(artist_credit, artist) if check_artist else 1.0
            b = similar(title, album)
            if a >= MIN_MATCH and b >= MIN_MATCH:
                scored.append((a + b, rg))

        if not scored:
            return None, "No MusicBrainz match"

        scored.sort(key=lambda x: -x[0])
        best_rg = scored[0][1]
        raw_date = best_rg.get("first-release-date")
        rg_id = best_rg.get("id")

        norm = normalize_date(raw_date)
        if not norm:
            return None, "MusicBrainz release-group had no date"

        year = parse_year(norm[:4])
        if not year:
            return None, "MusicBrainz had invalid year"

        if self.year_only:
            return str(year), f"MusicBrainz release-group {rg_id}"

        if len(norm) == 4:
            date = self._full_date_from_releases(rg_id, year)
            note = f"MusicBrainz release-group {rg_id}" + ("" if len(date) > 4 else " (year only)")
            return date, note

        return norm, f"MusicBrainz release-group {rg_id}"


# ==========================================
# ITUNES / APPLE MUSIC LOOKUP (BACKUP 2)
# ==========================================
class ITunesDateFinder(BaseFinder):
    def __init__(self, throttle, year_only=False):
        super().__init__(throttle)
        self.year_only = year_only

    def find_date(self, artist, album):
        self.last_error = None
        if not album:
            return None, "no album title"

        term = f"{artist} {album}".strip() if artist and not is_various(artist) else album.strip()
        params = {
            "term": term,
            "entity": "album",
            "limit": 10,
        }
        data = self._fetch_json(ITUNES_API, params=params)
        results = (data or {}).get("results") or []
        scored = []
        for r in results:
            col_name = r.get("collectionName", "")
            art_name = r.get("artistName", "")
            check_artist = bool(artist) and not is_various(artist)
            a = similar(art_name, artist) if check_artist else 1.0
            b = similar(col_name, album)
            if a >= MIN_MATCH and b >= MIN_MATCH:
                scored.append((a + b, r))

        if not scored:
            return None, "No iTunes match"

        scored.sort(key=lambda x: -x[0])
        best = scored[0][1]
        raw_date = best.get("releaseDate")  # ISO format: "YYYY-MM-DDTHH:MM:SSZ"
        norm = normalize_date(raw_date)
        if not norm:
            return None, "iTunes album had no date"

        col_id = best.get("collectionId")
        if self.year_only:
            return norm[:4], f"iTunes album {col_id}"
        return norm, f"iTunes album {col_id}"


# ==========================================
# APPLICATION
# ==========================================
REPORT_COLUMNS = ("mode", "folder", "file", "artist", "album", "old_date", "new_date", "action", "source")


class DateTaggerApp:
    def __init__(self, target_dir, args, needs_finders=True):
        self.target_dir = os.path.abspath(target_dir)
        self.args = args
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.state_file = os.path.join(self.target_dir, "date_state.jsonl")
        self.fail_file = os.path.join(self.target_dir, "failed_date_searches.txt")
        self.cache_file = os.path.join(self.target_dir, "date_cache.json")
        self.undo_file = os.path.join(self.target_dir, "date_undo.jsonl")

        self.is_dry_run = args.dry_run or args.dry_run_cached
        self.allow_cache_write = (not self.is_dry_run) or args.dry_run_cached
        self.use_cache = not args.refresh_cache

        self.report_path = args.report or (
            os.path.join(self.target_dir, f"date_dry_run_{self.run_id}.csv") if self.is_dry_run else None)
        self._report_file = self._report_writer = None

        self.throttle = HostThrottle(RATE_LIMITS)
        use_all = not (args.discogs_only or args.musicbrainz_only or args.itunes_only)

        self.discogs_finder = (DiscogsDateFinder(self.throttle, year_only=args.year_only, master_only=args.master_only)
                               if (needs_finders and (use_all or args.discogs_only)) else None)
        self.mb_finder = (MusicBrainzDateFinder(self.throttle, year_only=args.year_only)
                          if (needs_finders and (use_all or args.musicbrainz_only)) else None)
        self.itunes_finder = (ITunesDateFinder(self.throttle, year_only=args.year_only)
                             if (needs_finders and (use_all or args.itunes_only)) else None)

        self.processed = set() if args.reprocess else self._load_history()
        self.cache = self._load_cache()
        self.cache_dirty = False
        self.overwrite_policy = "all" if args.overwrite else "none" if args.no_overwrite else None
        self.stats = Counter()

    def _key(self, folder):
        return os.path.normcase(os.path.relpath(folder, self.target_dir))

    def _load_history(self):
        last_status = {}
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("schema", SCHEMA_VERSION) == SCHEMA_VERSION:
                            last_status[rec["folder"]] = rec["status"]
                    except (ValueError, KeyError, TypeError):
                        continue
        return {k for k, s in last_status.items() if s in TERMINAL_STATUSES}

    def _record(self, root, status, **extra):
        self.stats[status] += 1
        if self.is_dry_run:
            return
        rec = {"folder": self._key(root), "status": status, "schema": SCHEMA_VERSION,
               "ts": datetime.now().isoformat(timespec="seconds"), **extra}
        with open(self.state_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _mark_failure(self, root, artist, album, note, elapsed, existing_dates=None):
        self._record(root, "failed", artist=artist, album=album)
        old = ", ".join(sorted(set(existing_dates))) if existing_dates else ""
        self._report(root, "", artist, album, old, "", "no date found", note or "")
        if self.is_dry_run:
            return
        with open(self.fail_file, 'a', encoding='utf-8') as f:
            f.write(f"No date found | Folder: {root} | Searched: {artist} - {album} | {note} | "
                    f"Total Time: {elapsed:.1f}s\n")

    def _load_cache(self):
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get("version") != SCHEMA_VERSION or not isinstance(data.get("albums"), dict):
                return {}
            return {k: tuple(v) for k, v in data["albums"].items()
                    if v and normalize_date(v[0]) == v[0]}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_cache(self):
        if not self.allow_cache_write or not self.cache_dirty:
            return
        tmp = self.cache_file + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({"version": SCHEMA_VERSION, "albums": self.cache}, f, ensure_ascii=False, indent=0)
        os.replace(tmp, self.cache_file)

    def _log_undo(self, path, old, new):
        rec = {"run": self.run_id, "ts": datetime.now().isoformat(timespec="seconds"),
               "file": os.path.relpath(path, self.target_dir), "old": old, "new": new}
        with open(self.undo_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _read_undo_log(self):
        writes, undone = [], set()
        if not os.path.exists(self.undo_file):
            return writes, undone
        with open(self.undo_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("undone"):
                        undone.add((rec["run"], rec["file"]))
                    else:
                        writes.append({"run": rec["run"], "ts": rec.get("ts", ""), "file": rec["file"],
                                       "old": rec.get("old"), "new": rec["new"]})
                except (ValueError, KeyError, TypeError):
                    continue
        return writes, undone

    def list_runs(self):
        writes, undone = self._read_undo_log()
        if not writes:
            print("No tag changes have been logged yet.")
            return
        runs = {}
        for w in writes:
            r = runs.setdefault(w["run"], {"files": 0, "pending": 0, "ts": w["ts"]})
            r["files"] += 1
            r["pending"] += (w["run"], w["file"]) not in undone
        print(f"{'RUN ID':<18}{'WHEN':<22}{'FILES':>7}{'CAN UNDO':>10}")
        for run_id, r in runs.items():
            print(f"{run_id:<18}{r['ts']:<22}{r['files']:>7}{r['pending']:>10}")

    def undo(self, which="last", force=False):
        dry = self.is_dry_run
        writes, undone = self._read_undo_log()
        pending = [w for w in writes if (w["run"], w["file"]) not in undone]
        if not pending:
            print("Nothing to undo.")
            return
        if which == "last":
            run = pending[-1]["run"]
            selected, scope = [w for w in pending if w["run"] == run], f"run {run}"
        elif which == "all":
            selected, scope = pending, "all runs"
        else:
            selected, scope = [w for w in pending if w["run"] == which], f"run {which}"
            if not selected:
                print(f"No undoable changes for run '{which}'. Use --list-runs to see run ids.")
                return

        print(f"\n[UNDO] {'(dry run) ' if dry else ''}{len(selected)} file(s) from {scope}")
        results, touched = Counter(), set()
        for w in reversed(selected):
            path = os.path.join(self.target_dir, w["file"])
            status = AudioFileHandler.restore_dates(path, w["old"], w["new"], force=force, dry_run=dry)
            results[status] += 1
            name = w["file"]
            if status == "restored":
                touched.add(os.path.dirname(path))
                print(f"  {'[DRY RUN] Would restore' if dry else '+ Restored'}: {name}")
                if not dry:
                    with open(self.undo_file, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({"run": w["run"], "file": w["file"], "undone": True,
                                            "ts": datetime.now().isoformat(timespec="seconds")}) + "\n")
            elif status == "changed":
                print(f"  ~ Skipped (date edited since tagging; use --force to restore anyway): {name}")
            else:
                print(f"  ! File no longer exists: {name}")

        for folder in sorted(touched):
            self._record(folder, "undone", run=scope)
        print(f"\n[UNDO] restored: {results['restored']}, skipped: {results['changed']}, missing: {results['missing']}")

    def _open_report(self):
        if not self.report_path:
            return
        self._report_file = open(self.report_path, 'w', newline='', encoding='utf-8-sig')
        self._report_writer = csv.writer(self._report_file)
        self._report_writer.writerow(REPORT_COLUMNS)
        print(f" [i] Writing change report to: {self.report_path}")

    def _report(self, root, filename, artist, album, old, new, action, source=""):
        if self._report_writer is None:
            return
        self._report_writer.writerow(["dry-run" if self.is_dry_run else "applied", self._key(root),
                                      filename, artist, album, old, new, action, source])
        self._report_file.flush()

    def _close_report(self):
        if self._report_file:
            self._report_file.close()
            self._report_file = self._report_writer = None

    @staticmethod
    def _folder_identity(root, readable):
        is_comp = bool(COMP_FOLDER_RE.search(os.path.basename(root).lower()))
        artist = album = ""
        for t in readable:
            if t["compilation"] == "1":
                is_comp = True
            if not (artist and album):
                a = t["albumartist"] or t["artist"]
                if a and t["album"]:
                    artist, album = a, t["album"]
        return artist, album, is_comp

    def run(self):
        self._open_report()
        try:
            for root, dirs, files in os.walk(self.target_dir):
                dirs.sort()
                if self._key(root) in self.processed:
                    continue
                audio_files = sorted(f for f in files if f.lower().endswith(SUPPORTED_EXTENSIONS))
                if audio_files:
                    self._process_folder(root, audio_files)
        finally:
            for finder in (self.discogs_finder, self.mb_finder, self.itunes_finder):
                if finder:
                    finder.close()
            self._save_cache()
            self._close_report()

    def _process_folder(self, root, audio_files):
        print(f"\n[SCAN] {root}")
        start = time.time()

        tags_by_file = {f: AudioFileHandler.read_metadata(os.path.join(root, f)) for f in audio_files}
        readable = [t for t in tags_by_file.values() if t is not None]
        if not readable:
            print("  [-] Skipped: no readable audio files.")
            self.stats["skipped"] += 1
            return

        albums = {clean_album_title(t["album"]).casefold() for t in readable if t["album"]}
        if len(albums) > 1 and not self.args.allow_mixed:
            print(f"  [-] Skipped: {len(albums)} different album titles in one folder "
                  "(use --allow-mixed to tag it anyway).")
            self._report(root, "", "", "", "", "", "skipped: mixed albums in folder")
            self.stats["skipped"] += 1
            return

        if self.args.only_missing and all(t["original_date"] or t["release_date"] for t in readable):
            print("  [-] Skipped: every file already has a date tag (--only-missing).")
            self.stats["already_tagged"] += 1
            return

        raw_artist, raw_album, is_compilation = self._folder_identity(root, readable)
        if not raw_artist or not raw_album:
            print("  [-] Skipped: No Artist/Album tags found.")
            self._report(root, "", raw_artist, raw_album, "", "", "skipped: no artist/album tags")
            self.stats["skipped"] += 1
            return

        cands = artist_candidates(raw_artist)
        primary_artist = cands[0]
        is_comp_album = is_compilation or is_various(primary_artist)
        cleaned = clean_album_title(raw_album)
        cache_key = f"{'' if is_comp_album else primary_artist.casefold()}|{cleaned.casefold()}"
        if self.args.year_only:
            cache_key += "|y"  # never mix year-only results with full dates

        if self.use_cache and cache_key in self.cache:
            date, cached_source = self.cache[cache_key]
            note = f"[CACHE] {cached_source}"
            print(f"  [*] Using cached date for '{primary_artist} - {cleaned}'")
        else:
            date, note = self._lookup(cands, raw_album, is_comp_album)
            if date:
                self.cache[cache_key] = (date, note)
                self.cache_dirty = True

        elapsed = time.time() - start
        if not date:
            print(f"  \033[91m[FAILURE] No original date found. ({note})\033[0m")
            existing = [t["original_date"] for t in readable if t["original_date"]]
            self._mark_failure(root, primary_artist, cleaned, note, elapsed, existing)
            return

        print(f"  \033[92m[SUCCESS]\033[0m Original release date: {date} [{note}]")
        self._apply_date(root, audio_files, tags_by_file, date, note, elapsed, raw_artist, raw_album)

    def _query_variants(self, finder, artists, variants):
        failure_note = None
        for cand in artists:
            for album in variants:
                date, note = finder.find_date(cand, album)
                if date:
                    return date, note
                failure_note = failure_note or note
        return None, failure_note

    def _lookup(self, cands, raw_album, is_comp_album):
        cleaned = clean_album_title(raw_album)
        variants = [cleaned]
        if raw_album.strip() and raw_album.strip() != cleaned:
            variants.append(raw_album.strip())
        artists = [""] if is_comp_album else cands

        # 1. Primary: Discogs
        discogs_failure = "Discogs skipped"
        if self.discogs_finder:
            date, note = self._query_variants(self.discogs_finder, artists, variants)
            if date:
                return date, note
            discogs_failure = note

        # 2. Secondary: MusicBrainz
        mb_failure = "MusicBrainz skipped"
        if self.mb_finder:
            date, note = self._query_variants(self.mb_finder, artists, variants)
            if date:
                return date, note
            mb_failure = note

        # 3. Tertiary: iTunes / Apple Music Store
        itunes_failure = "iTunes skipped"
        if self.itunes_finder:
            date, note = self._query_variants(self.itunes_finder, artists, variants)
            if date:
                return date, note
            itunes_failure = note

        return None, f"Discogs: {discogs_failure} | MusicBrainz: {mb_failure} | iTunes: {itunes_failure}"

    def _ask_overwrite(self, differing, date):
        if self.args.only_missing or self.overwrite_policy == "none":
            return False
        if self.overwrite_policy == "all":
            return True
        distinct = sorted(set(differing))
        shown = ", ".join(f"'{d}'" for d in distinct[:3]) + (", ..." if len(distinct) > 3 else "")
        ans = timed_prompt(
            f"\033[93m      ? Overwrite {shown} with '{date}'? "
            f"[y]es / [n]o / [a]ll folders / [s]kip all (5s): \033[0m", 5)
        if ans in ("a", "all"):
            self.overwrite_policy = "all"
            return True
        if ans in ("s", "skip"):
            self.overwrite_policy = "none"
            return False
        return ans in ("y", "yes")

    def _apply_date(self, root, audio_files, tags_by_file, date, service, elapsed, artist, album):
        source = service
        plan, differing, errors = [], [], 0
        for f in audio_files:
            tags = tags_by_file[f]
            if tags is None:
                print(f"      ! Unreadable, skipped: {f}")
                errors += 1
                continue
            orig, rel = tags["original_date"], tags["release_date"]
            if (orig or rel) and self.args.only_missing:
                continue
            target = AudioFileHandler.effective_date(os.path.join(root, f), date)
            if date_matches(orig, target) and date_matches(rel, target):
                print(f"      ~ Skipped (Already matches '{orig}'): {f}")
                self._report(root, f, artist, album, orig, date, "already matches", source)
                continue
            # Only values that disagree count as "existing"; an empty or already-correct
            # field is simply filled in without prompting.
            stale = sorted({v for v in (orig, rel) if v and not date_matches(v, target)})
            differing.extend(stale)
            plan.append((f, ", ".join(stale)))

        preview_only = bool(differing) and self.is_dry_run and self.overwrite_policy is None
        overwrite = True if preview_only else (bool(differing) and self._ask_overwrite(differing, date))

        modified = 0
        for f, existing in plan:
            path = os.path.join(root, f)
            if existing and not overwrite:
                print(f"      ~ Skipped (kept '{existing}'): {f}")
                self._report(root, f, artist, album, existing, date, "kept existing", source)
                continue
            if self.is_dry_run:
                if existing:
                    note = " (you would be asked first)" if preview_only else ""
                    print(f"      [DRY RUN] Would overwrite '{existing}' with '{date}'{note}: {f}")
                    action = "would overwrite (asks first)" if preview_only else "would overwrite"
                else:
                    print(f"      [DRY RUN] Would tag with '{date}': {f}")
                    action = "would tag"
                self._report(root, f, artist, album, existing, date, action, source)
                modified += 1
                continue
            try:
                previous = AudioFileHandler.write_dates(path, date)
            except Exception as exc:
                print(f"      ! Error saving {f}: {exc}")
                errors += 1
                continue
            try:
                self._log_undo(path, previous, AudioFileHandler.effective_date(path, date))
            except OSError as exc:
                print(f"      ! Tagged, but could not write the undo log entry: {exc}")
            print(f"      + Overwrote with '{date}': {f}" if existing else f"      + Tagged: {f}")
            self._report(root, f, artist, album, existing, date, "overwrote" if existing else "tagged", source)
            modified += 1

        if errors:
            status = "error"
        elif modified:
            status = "tagged"
        elif differing:
            status = "declined"
        else:
            status = "matched"
        self._record(root, status, date=date, source=source, seconds=round(elapsed, 1))


# ==========================================
# SELF-TEST (offline)
# ==========================================
def run_selftest():
    import tempfile
    import wave
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"  FAIL {label}: got {got!r}, expected {want!r}")

    for raw, want in {
        "1973-03-01": "1973-03-01", "1973-03-00": "1973-03", "1973-00-00": "1973", "1973": "1973",
        "1973-3-1": "1973-03-01", "1973-13-01": "1973", "0": None, "": None, None: None,
    }.items():
        check(f"normalize_date({raw!r})", normalize_date(raw), want)

    check("pick_best_date mixed", pick_best_date(["1973", "1973-03", "1973-03-01"]), "1973-03-01")

    # Offline fake iTunes test
    class FakeITunes(ITunesDateFinder):
        def _fetch_json(self, url, params=None, headers=None):
            return {
                "results": [{
                    "collectionId": 99999,
                    "collectionName": "dragged To HELL!",
                    "artistName": "Kenny Morningstar",
                    "releaseDate": "2026-05-29T07:00:00Z"
                }]
            }

    throttle = HostThrottle(RATE_LIMITS)
    itunes = FakeITunes(throttle)
    res_date, res_src = itunes.find_date("Kenny Morningstar", "dragged To HELL!")
    check("iTunes fake lookup date", res_date, "2026-05-29")
    check("iTunes fake lookup source", "99999" in res_src, True)

    check("date_matches year in full", date_matches("1973-03-01", "1973"), True)
    check("date_matches year vs full", date_matches("1973", "1973-03-01"), False)
    check("lucene_escape", lucene_escape('12" \\ x'), '12\\" \\\\ x')

    with tempfile.TemporaryDirectory() as tmp:
        # Real (silent) MPEG frames are needed or mutagen cannot identify the file.
        for ver in (4, 3):
            path = os.path.join(tmp, f"t{ver}.mp3")
            with open(path, "wb") as f:
                f.write((bytes([0xFF, 0xFB, 0x90, 0x64]) + b"\x00" * 413) * 20)
            ID3().save(path, v2_version=ver)
            want = "1973-03-01" if ver == 4 else "1973"
            prev = AudioFileHandler.write_dates(path, "1973-03-01")
            check(f"v2.{ver} effective", AudioFileHandler.effective_date(path, "1973-03-01"), want)
            meta = AudioFileHandler.read_metadata(path)
            check(f"v2.{ver} readback orig", meta["original_date"] if meta else None, want)
            check(f"v2.{ver} readback rel", meta["release_date"] if meta else None, want)

            raw = ID3(path, translate=False)  # translate=False: see the frames as stored
            if ver == 3:
                check("v2.3 TORY frame count is 1", len(raw.getall("TORY")), 1)
                check("v2.3 TDOR frame eliminated", "TDOR" in raw, False)

            status = AudioFileHandler.restore_dates(path, prev, AudioFileHandler.effective_date(path, "1973-03-01"))
            meta = AudioFileHandler.read_metadata(path)
            check(f"v2.{ver} undo status", status, "restored")
            check(f"v2.{ver} undo clears date", meta["original_date"] if meta else None, "")

        # WAV must stay a valid RIFF file (regression: ID3.save() used to corrupt it)
        path = os.path.join(tmp, "t.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        AudioFileHandler.write_dates(path, "1973-03-01")
        with open(path, "rb") as f:
            check("wav keeps RIFF header", f.read(4), b"RIFF")
        meta = AudioFileHandler.read_metadata(path)
        check("wav readback orig", meta["original_date"] if meta else None, "1973-03-01")
        try:
            with wave.open(path):
                pass
        except Exception as exc:  # noqa: BLE001
            failures.append(f"  FAIL wav no longer readable: {exc}")

    print("\n".join(failures) if failures else "All self-tests passed.")
    return 1 if failures else 0


# ==========================================
# CLI
# ==========================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="OriginalDateShark - write Discogs, MusicBrainz & iTunes original release dates to audio files")
    p.add_argument("path", nargs="?", help="Folder containing your audio library (prompted if omitted)")
    p.add_argument("--dry-run", action="store_true",
                   help="Look up dates and write a CSV report of proposed changes; change nothing else")
    p.add_argument("--dry-run-cached", action="store_true",
                   help="Like --dry-run, but allows saving results to date_cache.json")
    p.add_argument("--report", metavar="FILE.csv",
                   help="Write a CSV of every change, proposed or applied")
    p.add_argument("--only-missing", action="store_true",
                   help="Only tag files with no date tags; folders where every file has one are skipped with no network calls")
    p.add_argument("--allow-mixed", action="store_true",
                   help="Tag folders that contain several different album titles (one date is applied to all files)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--overwrite", action="store_true", help="Overwrite existing original dates without asking")
    g.add_argument("--no-overwrite", action="store_true", help="Never overwrite existing original dates (no prompts)")
    p.add_argument("--reprocess", action="store_true", help="Ignore saved progress and rescan every folder")
    p.add_argument("--refresh-cache", action="store_true",
                   help="Ignore cached results and look everything up again (cache is updated)")
    p.add_argument("--year-only", action="store_true",
                   help="Write just the year (fastest: skips the extra version/release lookups)")
    p.add_argument("--master-only", action="store_true",
                   help="For Discogs: only accept master releases; don't fall back to earliest release")
    g_backend = p.add_mutually_exclusive_group()
    g_backend.add_argument("--discogs-only", action="store_true", help="Only query Discogs")
    g_backend.add_argument("--musicbrainz-only", action="store_true", help="Only query MusicBrainz")
    g_backend.add_argument("--itunes-only", action="store_true", help="Only query iTunes / Apple Music")
    p.add_argument("--undo", action="store_true", help="Revert the most recent tagging run")
    p.add_argument("--undo-run", metavar="RUN_ID|all", help="Revert one specific run (see --list-runs), or 'all'")
    p.add_argument("--list-runs", action="store_true", help="List logged runs that can still be undone")
    p.add_argument("--force", action="store_true",
                   help="With --undo: restore even if the date was edited after tagging")
    p.add_argument("-v", "--verbose", action="store_true", help="Show request errors and debug detail")
    p.add_argument("--selftest", action="store_true", help="Run offline sanity checks and exit")
    return p.parse_args(argv)


def format_duration(seconds):
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{int(hours)}h {int(mins)}m {secs:.1f}s"
    if mins > 0:
        return f"{int(mins)}m {secs:.1f}s"
    return f"{secs:.1f} seconds"


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="      (debug) %(message)s")
    if args.selftest:
        sys.exit(run_selftest())

    undo_mode = args.undo or bool(args.undo_run)
    print("=" * 60)
    print(" ORIGINALDATESHARK - MULTI-FORMAT AUDIO TAGGER ")
    print("=" * 60)

    if not (undo_mode or args.list_runs):
        if ensure_config_template():
            print(f"\033[92m [+] Created an empty settings template: {CONFIG_PATH}\033[0m")
        if not DISCOGS_TOKEN and not (args.musicbrainz_only or args.itunes_only):
            print("\033[93m [!] DISCOGS_TOKEN not set; will fall back to MusicBrainz & iTunes.\033[0m")

    target = (args.path or input("Enter the full path to your audio folder: ")).strip().strip('"').strip("'")
    if not os.path.isdir(target):
        print(f"Error: The directory '{target}' does not exist.")
        sys.exit(1)

    if args.list_runs or undo_mode:
        app = DateTaggerApp(target, args, needs_finders=False)
        if args.list_runs:
            app.list_runs()
        else:
            app.undo(which=args.undo_run or "last", force=args.force)
        return

    app = DateTaggerApp(target, args)
    print(f"Run ID: {app.run_id}")
    if app.is_dry_run:
        print("\n[DRY RUN] No tags, state or undo files will be written; only the CSV report.")
        if args.dry_run_cached:
            print("          Cache updates ARE enabled (--dry-run-cached).")
    print(f"\nStarting scan over supported formats: {', '.join(SUPPORTED_EXTENSIONS)}")
    print("(Press Ctrl+C to stop; finished folders are remembered)")

    start = time.time()
    interrupted = False
    try:
        app.run()
    except KeyboardInterrupt:
        interrupted = True
        print("\n\n[!] Interrupted. Progress for completed folders has been saved.")

    print("\n" + "=" * 60)
    print(" SCAN INTERRUPTED" if interrupted else " SCAN COMPLETE")
    if app.stats:
        print(" " + ", ".join(f"{k}: {v}" for k, v in sorted(app.stats.items())))
    if not app.is_dry_run and (app.stats["tagged"] or app.stats["error"]):
        print(f" Undo this run with: python {os.path.basename(__file__)} \"{target}\" --undo      (run id {app.run_id})")
    print(f" Grand Total Time: {format_duration(time.time() - start)}")
    print("=" * 60)


if __name__ == "__main__":
    main()