# Google Watched to Trakt

Import movies and TV shows marked as watched in Google Search and Google TV into
your [Trakt](https://trakt.tv/) watch history.

The importer:

- reads `Search Contributions/Watched.json` from a Google Takeout ZIP, tarball,
  or extracted directory;
- searches Trakt using the exported titles;
- automatically accepts only unique exact-title matches;
- puts ambiguous and unmatched records in `report.json` for review;
- uses Google’s original watched timestamps;
- avoids adding titles that are already marked watched in Trakt;
- imports in small batches so large histories do not time out; and
- can optionally request the data through Google’s Data Portability API where
  that API is available.

## Important behavior

When a Google record uniquely matches a Trakt TV show, committing the import
marks every currently aired episode of that show as watched. This is how
Trakt’s whole-show history endpoint works.

Ambiguous and unmatched records are not imported automatically. Review
`report.json` before committing.

## Requirements

- Python 3.10 or newer; no third-party Python packages are required.
- A Trakt account and a Trakt API application.
- A Google Takeout archive containing **Search Contributions**, or an eligible
  Google Data Portability API project.

## 1. Export your Google watched data

For most users:

1. Open [Google Takeout](https://takeout.google.com/).
2. Choose **Deselect all**.
3. Select **Search Contributions**.
4. Create and download the export.

The archive should contain `Search Contributions/Watched.json`.

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

## 4. Commit exact matches to Trakt

After reviewing the report:

```bash
python3 import_watched.py --takeout ~/Downloads/takeout-*.zip --commit
```

Open the displayed Trakt activation page, enter the device code, and approve
access. The importer then skips existing watched titles and adds the remaining
exact matches.

Running the command again is safe: the importer reads your complete paginated
Trakt watched state before sending changes.

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
- Google Takeout has occasionally emitted duplicate timestamps incorrectly
  encoded near January 1970. The importer repairs these timestamps and removes
  the damaged copy when a correct duplicate exists.

## License

[MIT](LICENSE)
