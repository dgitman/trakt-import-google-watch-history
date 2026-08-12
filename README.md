# Google Watched to Trakt

Import movies and TV shows marked as watched in Google Search and Google TV into
your [Trakt](https://trakt.tv/) watch history.

The importer:

- reads `Search Contributions/Watched.json` from a Google Takeout ZIP, tarball,
  or extracted directory;
- reads `My Activity/Google TV/MyActivity.html` from Google Takeout;
- searches Trakt using the exported titles;
- automatically accepts only unique exact-title matches;
- puts ambiguous and unmatched records in `report.json` for review;
- uses Google’s original watched timestamps;
- avoids adding titles that are already marked watched in Trakt;
- imports in small batches so large histories do not time out; and
- can optionally request the data through Google’s Data Portability API where
  that API is available.

## Important behavior

For Search Contributions, a unique Trakt TV-show match is treated as a whole
show and committing it marks every currently aired episode watched.

Google TV My Activity names a show but does not identify the episode. To avoid
marking an entire series accidentally, the Google TV activity workflow imports
only exact movie matches. Exact show-title matches remain in the report for
manual review.

Ambiguous and unmatched records are not imported automatically. Review
`report.json` before committing.

## Requirements

- Python 3.10 or newer; no third-party Python packages are required.
- A Trakt account and a Trakt API application.
- A Google Takeout archive containing **Search Contributions** or **Google TV
  My Activity**, or an eligible Google Data Portability API project.

## 1. Export your Google watched data

For most users:

1. Open [Google Takeout](https://takeout.google.com/).
2. Choose **Deselect all**.
3. Select **Search Contributions**.
4. Create and download the export.

The archive should contain `Search Contributions/Watched.json`.

For Google TV activity, export **My Activity** and locate:

```text
Takeout/My Activity/Google TV/MyActivity.html
```

Google’s Data Portability API is available only in supported countries and
regions. Google directs users elsewhere, including users with US-associated
accounts, to use Takeout instead.

## 2. Create a Trakt API application

Create an application at <https://trakt.tv/oauth/applications>. Copy its client
ID and client secret into `.env.trakt`:

```dotenv
TRAKT_CLIENT_ID=your_client_id
TRAKT_CLIENT_SECRET=your_client_secret
```

The importer uses Trakt’s device authorization flow, so it will display an
activation URL and code when you commit your first import.

## 3. Preview the import

Clone this repository and run:

```bash
python3 import_watched.py --takeout ~/Downloads/takeout-*.zip
```

This performs read-only Trakt searches and creates `report.json`. The summary
shows the number of unique exact matches, ambiguous matches, and missing
records. Inspect the report before continuing.

For Google TV My Activity, run:

```bash
python3 import_watched.py \
  --google-tv-activity ~/Downloads/Takeout/My\ Activity/Google\ TV/MyActivity.html
```

This creates `google-tv-report.json`. Ads, generic labels such as `Media`, and
year-only labels are excluded so they cannot become false Trakt matches.

## 4. Commit exact matches to Trakt

After reviewing the report:

```bash
python3 import_watched.py --takeout ~/Downloads/takeout-*.zip --commit
```

Open the displayed Trakt activation page, enter the device code, and approve
access. The importer then skips existing watched titles and adds the remaining
exact matches.

Running the command again is safe: the importer reads your complete paginated
Trakt watched state and confirms apparent misses against direct movie history
before sending changes. Expired Trakt access tokens are refreshed automatically.

To commit a reviewed Google TV report directly (for example, if the Takeout
file has been moved), run:

```bash
python3 import_watched.py --google-tv-report google-tv-report.json --commit
```

## Optional: Google Data Portability API

The direct API flow requires a Google Cloud project with billing, the Data
Portability API enabled, an OAuth client configured with
`http://127.0.0.1:8765/callback`, and the following scope:

```text
https://www.googleapis.com/auth/dataportability.search_ugc.media.watched
```

Put the OAuth values in `.env.google`:

```dotenv
GOOGLE_OAUTH_CLIENT_ID=your_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_client_secret
```

Then run without `--takeout`:

```bash
python3 import_watched.py
```

Google archive jobs are asynchronous. If the archive is still in progress,
wait at least five minutes and run the command again.

## Privacy and local files

Watch history, reports, downloaded archives, OAuth tokens, credentials, and
generated review CSVs are ignored by Git. Do not publish these files. Tokens
are stored locally under `.state/`.

## Limitations

- Google exports titles but usually does not include stable movie or show IDs.
  Remakes and same-name media therefore require manual review.
- Google may export episode titles. This version searches movies and shows, so
  those episode records can remain unmatched or require manual handling.
- Google TV show activity has no episode number, so it is never imported as a
  whole show automatically.
- Google Takeout has occasionally emitted duplicate timestamps incorrectly
  encoded near January 1970. The importer repairs these timestamps and removes
  the damaged copy when a correct duplicate exists.

## License

[MIT](LICENSE)
