# DateShark

Look up the **original** release date of every album in your local music library and write it into the tags.

For each album folder, DateShark searches **Discogs → MusicBrainz → iTunes/Apple Music** (first hit wins) and writes the date to both:

1. the *Original Release Date* frame (`TDOR`/`TORY`, `ORIGINALDATE`, or `©day`), and
2. the primary *Year / Date* frame (`TDRC`/`TYER`, `DATE`, or `©day`), which is what Mp3tag and most players show.

Supported formats: **MP3, FLAC, OGG, OPUS, M4A/MP4, WAV, AIFF**.

## Features

- Three-service lookup cascade with fuzzy artist/album matching (accents, `feat.`, `Vol.`, "The …", remaster suffixes, compilations / Various Artists).
- Full dates (`YYYY-MM-DD`) when available, `--year-only` for speed.
- ID3v2.3 files get native `TORY` + `TYER` frames, so no `2007\2007` duplicates in Mp3tag; v2.4 files get `TDOR` + `TDRC`.
- **Dry run** with a CSV report of every proposed change.
- **Undo** by run (`--undo`, `--undo-run`, `--list-runs`); undo refuses to touch files you edited afterwards unless you add `--force`.
- Resumable: finished folders are remembered; results are cached; per-host rate limiting and 429 back-off.

## Install

Requires Python 3.8+.

```bash
git clone https://github.com/<your-user>/OriginalDateShark.git
cd OriginalDateShark
pip install -r requirements.txt
```

### Discogs token (recommended)

Discogs is the primary source and needs a free personal token
([discogs.com/settings/developers](https://www.discogs.com/settings/developers) → *Generate new token*).
Without one, the tool falls back to MusicBrainz and iTunes.

## Usage

```bash
# 1. Preview. Changes nothing except a CSV report in the music folder.
python DateShark.py "/path/to/Music" --dry-run

# 2. Tag for real (asks before overwriting an existing, different date)
python DateShark.py "/path/to/Music"

# 3. Changed your mind?
python DateShark.py "/path/to/Music" --undo
```

**Back up your library before the first real run.** The tool writes tags in place.

Your library should be organised **one album per folder**. Folders that contain several different album titles are skipped (see `--allow-mixed`).

### Options

| Option | Description |
|---|---|
| `--dry-run` | Look up dates and write a CSV report; modify nothing else |
| `--dry-run-cached` | Like `--dry-run`, but also saves lookups to `date_cache.json` |
| `--report FILE.csv` | Write a CSV of every change, proposed or applied |
| `--only-missing` | Only tag files that have no date tag; folders where every file has one are skipped without network calls |
| `--overwrite` / `--no-overwrite` | Always / never replace existing different dates (no prompts) |
| `--year-only` | Write just the year; skips the extra lookups |
| `--master-only` | Discogs: accept only master releases, no fallback to the earliest release |
| `--discogs-only` / `--musicbrainz-only` / `--itunes-only` | Use a single source |
| `--allow-mixed` | Tag folders with several album titles (one date goes to every file) |
| `--reprocess` | Ignore saved progress and rescan every folder |
| `--refresh-cache` | Ignore cached lookups |
| `--undo` / `--undo-run ID\|all` / `--list-runs` | Revert the last run / a specific run / list undoable runs |
| `--force` | With undo: restore even if the date was edited since |
| `-v`, `--verbose` | Show request errors and debug detail |
| `--selftest` | Run offline sanity checks and exit |

When a file already has a different date you are asked: `[y]es / [n]o / [a]ll folders / [s]kip all`. No answer within 5 seconds counts as *no*.

## Files created in your music folder

| File | Purpose |
|---|---|
| `date_state.jsonl` | Which folders are done (makes runs resumable) |
| `date_undo.jsonl` | Old/new values for every file written (powers `--undo`) |
| `date_cache.json` | Cached lookups |
| `failed_date_searches.txt` | Albums no source could find |
| `date_dry_run_<id>.csv` | Dry-run report |

## How the date is chosen

- **Discogs:** the master release year, refined to a full date from the earliest version released that year.
- **MusicBrainz:** the release group's `first-release-date`, refined from its releases when only a year is given.
- **iTunes:** the album's `releaseDate`. This is the *store* release date and is often a remaster or reissue date, so it is the least reliable source. Review iTunes-sourced rows in the dry-run CSV (the `source` column) or use `--discogs-only` / `--musicbrainz-only`.

Matches need a similarity of at least 0.7 for both artist and album. Albums with unusual titles can miss; check `failed_date_searches.txt`.

## Known limitations

- One date per folder; files inside a folder are not matched individually.
- `ORIGINALYEAR` Vorbis comments are read but not rewritten.
- Undo of an ID3v2.3 MP3 restores years only (that format's frames are year-only).
- Dates are not checked against the calendar (e.g. `02-31` from a source would be written as-is).
- The tool writes its state files into the library folder.

## Credits and data sources

Uses [mutagen](https://mutagen.readthedocs.io/) and [requests](https://requests.readthedocs.io/).
Data comes from [Discogs](https://www.discogs.com/developers), [MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API), and the [iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/). Please respect their terms and rate limits.
This project is not affiliated with any of them.

## License

[MIT](LICENSE)
