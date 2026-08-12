#!/usr/bin/env python3
"""Import Google Search/Google TV watched marks into Trakt history."""

from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
DOWNLOADS = ROOT / "downloads"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/dataportability.search_ugc.media.watched"
RESOURCE = "search_ugc.media.watched"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip("'\""))


def request_json(url: str, *, method="GET", headers=None, body=None, return_headers=False):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.load(response)
            return (payload, response.headers) if return_headers else payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc


def google_token() -> dict:
    STATE.mkdir(exist_ok=True)
    token_file = STATE / "google-token.json"
    if token_file.exists():
        token = json.loads(token_file.read_text())
        if token.get("expires_at", 0) > time.time() + 120:
            return token
        if token.get("refresh_token"):
            refreshed = urllib.parse.urlencode({
                "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token",
            }).encode()
            req = urllib.request.Request("https://oauth2.googleapis.com/token", data=refreshed)
            with urllib.request.urlopen(req) as response:
                update = json.load(response)
            token.update(update)
            token["expires_at"] = time.time() + update["expires_in"]
            token_file.write_text(json.dumps(token, indent=2))
            return token

    state = secrets.token_urlsafe(24)
    redirect = "http://127.0.0.1:8765/callback"
    params = {
        "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")
        def log_message(self, *_):
            pass

    print("Opening Google authorization. If no browser opens, visit:\n" + auth_url, flush=True)
    webbrowser.open(auth_url)
    with socketserver.TCPServer(("127.0.0.1", 8765), Handler) as server:
        server.timeout = 300
        server.handle_request()
    if result.get("state") != state or "code" not in result:
        raise RuntimeError(f"Google authorization failed: {result}")
    payload = urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        "code": result["code"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=payload)
    with urllib.request.urlopen(req) as response:
        token = json.load(response)
    token["expires_at"] = time.time() + token["expires_in"]
    token_file.write_text(json.dumps(token, indent=2))
    return token


def google_archive(token: str) -> list[Path]:
    headers = {"Authorization": f"Bearer {token}"}
    job_file = STATE / "google-job.json"
    if job_file.exists():
        job = json.loads(job_file.read_text())
    else:
        job = request_json(
            "https://dataportability.googleapis.com/v1/portabilityArchive:initiate",
            method="POST", headers=headers, body={"resources": [RESOURCE]},
        )
        job_file.write_text(json.dumps(job, indent=2))
    job_id = job.get("archiveJobId") or job.get("jobId") or job.get("job_id")
    if not job_id:
        raise RuntimeError(f"No archive job ID in response: {job}")
    state = request_json(
        f"https://dataportability.googleapis.com/v1/archiveJobs/{job_id}/portabilityArchiveState",
        headers=headers,
    )
    print("Google archive state:", state.get("state"))
    if state.get("state") != "COMPLETE":
        print("Run this command again later; Google recommends polling every 5-60 minutes.")
        return []
    DOWNLOADS.mkdir(exist_ok=True)
    paths = []
    for index, url in enumerate(state.get("urls", []), 1):
        path = DOWNLOADS / f"google-watched-{index}"
        with urllib.request.urlopen(url, timeout=300) as response:
            path.write_bytes(response.read())
        paths.append(path)
    return paths


def extract_json(paths: list[Path]) -> list[dict]:
    expanded = []
    for path in paths:
        if path.is_dir():
            expanded.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
        else:
            expanded.append(path)
    files = []
    for path in expanded:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.lower().endswith(".json"):
                        files.append(json.loads(archive.read(name)))
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    if member.name.lower().endswith(".json"):
                        files.append(json.load(archive.extractfile(member)))
        else:
            try:
                files.append(json.loads(path.read_text()))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
    rows = []
    def repair_published(value):
        """Repair Takeout timestamps whose Unix value was divided by 1000."""
        if not isinstance(value, str) or not value.startswith("1970-01-"):
            return value, False
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            repaired = dt.datetime.fromtimestamp(parsed.timestamp() * 1000, dt.timezone.utc)
            if 2000 <= repaired.year <= dt.datetime.now(dt.timezone.utc).year + 1:
                return repaired.isoformat(timespec="seconds").replace("+00:00", "Z"), True
        except (ValueError, OverflowError, OSError):
            pass
        return value, False

    def walk(value):
        if isinstance(value, dict):
            query = value.get("Search Query") or value.get("searchQuery")
            published = value.get("Published") or value.get("published")
            if query:
                published, damaged = repair_published(published)
                rows.append({"title": str(query), "watched_at": published, "_damaged": damaged})
            else:
                for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    for obj in files: walk(obj)
    # Takeout sometimes includes a correct record plus a duplicate whose Unix
    # timestamp was divided by 1000. Discard the damaged copy when its correct
    # counterpart is present within the lost 1,000-second precision window.
    valid_by_title = {}
    for row in rows:
        if not row["_damaged"] and row.get("watched_at"):
            try:
                stamp = dt.datetime.fromisoformat(row["watched_at"].replace("Z", "+00:00")).timestamp()
                valid_by_title.setdefault(row["title"], []).append(stamp)
            except ValueError:
                pass
    filtered = []
    for row in rows:
        if row["_damaged"]:
            repaired = dt.datetime.fromisoformat(row["watched_at"].replace("Z", "+00:00")).timestamp()
            if any(0 <= candidate - repaired < 1000 for candidate in valid_by_title.get(row["title"], [])):
                continue
        row.pop("_damaged", None)
        filtered.append(row)
    rows = filtered
    unique = {(r["title"], r.get("watched_at")): r for r in rows}
    return list(unique.values())


def extract_google_tv_activity(path: Path) -> list[dict]:
    """Extract identifiable non-ad `Watched` events from Google TV My Activity."""
    source = path.read_text(errors="replace")
    marker = '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
    latest = {}
    excluded = {"Media", "Google TV", "Free channels from Google TV"}
    for block in source.split(marker)[1:]:
        title_match = re.search(r"Watched(?:&nbsp;|\s|\xa0)+(.*?)<br>", block, re.S)
        if not title_match or "From Google Ads" in block:
            continue
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", title_match.group(1)))
        title = re.sub(r"\s+", " ", title).strip()
        # Year-only labels appear in My Activity and can accidentally match
        # unrelated Trakt movies whose titles are just a year.
        if not title or title in excluded or re.fullmatch(r"\d{4}", title):
            continue
        dates = re.findall(
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
            r"\d{1,2}, \d{4}, \d{1,2}:\d{2}:\d{2}[^<]*?(?:EST|EDT))<br>",
            block,
        )
        if not dates:
            continue
        local_text = html_lib.unescape(dates[-1]).replace("\u202f", " ").replace("\xa0", " ")
        local_text = re.sub(r"\s+(?:EST|EDT)$", "", local_text)
        local = dt.datetime.strptime(local_text, "%b %d, %Y, %I:%M:%S %p")
        watched_at = local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(dt.timezone.utc)
        row = {
            "title": title,
            "watched_at": watched_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        if title not in latest or row["watched_at"] > latest[title]["watched_at"]:
            latest[title] = row
    return list(latest.values())


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def trakt_headers(token=None):
    headers = {
        "trakt-api-key": os.environ["TRAKT_CLIENT_ID"],
        "trakt-api-version": "2",
        "User-Agent": "Google-Watched-to-Trakt/1.0",
    }
    if token: headers["Authorization"] = f"Bearer {token}"
    return headers


def trakt_token() -> str:
    STATE.mkdir(exist_ok=True)
    path = STATE / "trakt-token.json"
    if path.exists():
        token = json.loads(path.read_text())
        expires_at = token.get("created_at", 0) + token.get("expires_in", 0)
        if expires_at > time.time() + 120:
            return token["access_token"]
        if not token.get("refresh_token"):
            raise RuntimeError("The cached Trakt token expired and has no refresh token.")
        refreshed = request_json(
            "https://api.trakt.tv/oauth/token",
            method="POST",
            headers=trakt_headers(),
            body={
                "refresh_token": token["refresh_token"],
                "client_id": os.environ["TRAKT_CLIENT_ID"],
                "client_secret": os.environ["TRAKT_CLIENT_SECRET"],
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token",
            },
        )
        path.write_text(json.dumps(refreshed, indent=2))
        return refreshed["access_token"]
    device = request_json("https://api.trakt.tv/oauth/device/code", method="POST",
                          headers=trakt_headers(), body={"client_id": os.environ["TRAKT_CLIENT_ID"]})
    print(f"Open {device['verification_url']} and enter code {device['user_code']}", flush=True)
    webbrowser.open(device["verification_url"])
    deadline = time.time() + device["expires_in"]
    while time.time() < deadline:
        time.sleep(device["interval"])
        try:
            token = request_json("https://api.trakt.tv/oauth/device/token", method="POST",
                headers=trakt_headers(), body={"code": device["device_code"],
                "client_id": os.environ["TRAKT_CLIENT_ID"],
                "client_secret": os.environ["TRAKT_CLIENT_SECRET"]})
            path.write_text(json.dumps(token, indent=2))
            return token["access_token"]
        except RuntimeError as exc:
            if "HTTP 400" not in str(exc): raise
    raise RuntimeError("Trakt device authorization expired")


def match(rows: list[dict], report_path: Path | None = None) -> dict:
    matched, ambiguous, missing = [], [], []
    cache_file = STATE / "trakt-search-cache.json"
    STATE.mkdir(exist_ok=True)
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    for i, row in enumerate(rows, 1):
        title = row["title"]
        if title in cache:
            results = cache[title]
        else:
            query = urllib.parse.quote(title)
            results = request_json(f"https://api.trakt.tv/search/movie,show?query={query}", headers=trakt_headers())
            cache[title] = results
            cache_file.write_text(json.dumps(cache))
        exact = []
        for result in results:
            media = result.get("movie") or result.get("show")
            if media and norm(media.get("title", "")) == norm(row["title"]):
                exact.append((result["type"], media))
        if len(exact) == 1:
            kind, media = exact[0]
            matched.append({"source": row, "type": kind, "title": media["title"],
                            "year": media.get("year"), "ids": media["ids"]})
        elif exact:
            ambiguous.append({"source": row,
                              "candidates": [{"type": kind, **media} for kind, media in exact]})
        else:
            missing.append(row)
        if i % 20 == 0: print(f"Matched {i}/{len(rows)}")
    report = {"matched": matched, "ambiguous": ambiguous, "missing": missing}
    (report_path or ROOT / "report.json").write_text(json.dumps(report, indent=2))
    return report


def sync(report: dict, token: str) -> dict:
    headers = trakt_headers(token)
    def all_watched(kind):
        items, page = [], 1
        while True:
            batch, response_headers = request_json(
                f"https://api.trakt.tv/users/me/watched/{kind}?extended=noseasons&limit=250&page={page}",
                headers=headers,
                return_headers=True,
            )
            items.extend(batch)
            page_count = int(response_headers.get("X-Pagination-Page-Count", page))
            if page >= page_count:
                return items
            page += 1

    requested_types = {item["type"] for item in report["matched"]}
    watched_movies = all_watched("movies") if "movie" in requested_types else []
    watched_shows = all_watched("shows") if "show" in requested_types else []
    existing_movies = {item["movie"]["ids"]["trakt"] for item in watched_movies}
    existing_shows = {item["show"]["ids"]["trakt"] for item in watched_shows}
    pending = {"movies": {}, "shows": {}}
    skipped_existing = {"movies": 0, "shows": 0}
    for item in report["matched"]:
        kind = "movies" if item["type"] == "movie" else "shows"
        trakt_id = item["ids"]["trakt"]
        existing = existing_movies if kind == "movies" else existing_shows
        # The aggregate watched endpoint can lag just after a history sync.
        # Confirm apparent movie misses against their direct history endpoint
        # before resubmitting them as timestamp updates.
        if kind == "movies" and trakt_id not in existing:
            history = request_json(
                f"https://api.trakt.tv/users/me/history/movies/{trakt_id}?limit=1",
                headers=headers,
            )
            if history:
                existing.add(trakt_id)
        if trakt_id in existing:
            skipped_existing[kind] += 1
            continue
        safe_ids = {key: value for key, value in item["ids"].items()
                    if key in {"trakt", "imdb", "tmdb", "tvdb", "slug"} and value is not None}
        candidate = {"watched_at": item["source"].get("watched_at") or dt.datetime.now(dt.timezone.utc).isoformat(),
                     "ids": safe_ids}
        previous = pending[kind].get(trakt_id)
        if not previous or candidate["watched_at"] > previous["watched_at"]:
            pending[kind][trakt_id] = candidate

    totals = {"added_movies": 0, "added_episodes": 0, "updated_movies": 0,
              "updated_episodes": 0, "not_found": {"movies": [], "shows": []}}
    for kind, size in (("movies", 50), ("shows", 5)):
        items = list(pending[kind].values())
        for start in range(0, len(items), size):
            batch = items[start:start + size]
            result = request_json("https://api.trakt.tv/sync/history", method="POST",
                                  headers=headers, body={kind: batch})
            totals["added_movies"] += result.get("added", {}).get("movies", 0)
            totals["added_episodes"] += result.get("added", {}).get("episodes", 0)
            totals["updated_movies"] += result.get("updated", {}).get("movies", 0)
            totals["updated_episodes"] += result.get("updated", {}).get("episodes", 0)
            for key in totals["not_found"]:
                totals["not_found"][key].extend(result.get("not_found", {}).get(key, []))
            print(f"Imported {min(start + size, len(items))}/{len(items)} {kind}", flush=True)
    totals["skipped_existing"] = skipped_existing
    return totals


def main():
    parser = argparse.ArgumentParser()
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--takeout", type=Path,
                        help="Search Contributions Takeout archive or extracted directory")
    inputs.add_argument("--google-tv-activity", type=Path,
                        help="Google TV My Activity HTML exported by Google Takeout")
    inputs.add_argument("--google-tv-report", type=Path,
                        help="Resume from a previously generated Google TV match report")
    parser.add_argument("--commit", action="store_true", help="Write reviewed matches to Trakt")
    args = parser.parse_args()
    load_env(ROOT / ".env.google")
    load_env(ROOT / ".env.trakt")
    required = ["TRAKT_CLIENT_ID"]
    if args.commit:
        token_file = STATE / "trakt-token.json"
        token_expired = True
        if token_file.exists():
            cached = json.loads(token_file.read_text())
            token_expired = cached.get("created_at", 0) + cached.get("expires_in", 0) <= time.time() + 120
        if not token_file.exists() or token_expired:
            required.append("TRAKT_CLIENT_SECRET")
    if not args.takeout and not args.google_tv_activity and not args.google_tv_report:
        required += ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"]
    absent = [name for name in required if not os.environ.get(name)]
    if absent: raise SystemExit("Missing variables: " + ", ".join(absent))
    if args.google_tv_report:
        report_path = args.google_tv_report.expanduser().resolve()
        if not report_path.exists():
            raise SystemExit(f"Google TV report path does not exist: {report_path}")
        report = json.loads(report_path.read_text())
        rows = None
    elif args.google_tv_activity:
        activity = args.google_tv_activity.expanduser().resolve()
        if not activity.exists():
            raise SystemExit(f"Google TV activity path does not exist: {activity}")
        rows = extract_google_tv_activity(activity)
        report_path = ROOT / "google-tv-report.json"
    elif args.takeout:
        takeout = args.takeout.expanduser().resolve()
        if not takeout.exists():
            raise SystemExit(f"Takeout path does not exist: {takeout}")
        downloads = [takeout]
        rows = extract_json(downloads)
        report_path = ROOT / "report.json"
    else:
        google = google_token()
        downloads = google_archive(google["access_token"])
        if not downloads: return
        rows = extract_json(downloads)
        report_path = ROOT / "report.json"
    if rows is not None:
        print(f"Found {len(rows)} Google watched records")
        report = match(rows, report_path)
    print(f"Exact: {len(report['matched'])}; ambiguous: {len(report['ambiguous'])}; missing: {len(report['missing'])}")
    if args.commit:
        import_report = report
        if args.google_tv_activity or args.google_tv_report:
            # The activity names a show but not its episode. Sending a show to
            # Trakt would mark every aired episode watched, so only exact movie
            # matches are safe to import automatically.
            movie_matches = [item for item in report["matched"] if item["type"] == "movie"]
            show_matches = len(report["matched"]) - len(movie_matches)
            import_report = {**report, "matched": movie_matches}
            print(
                f"Google TV activity: importing {len(movie_matches)} exact movie matches; "
                f"leaving {show_matches} title-only show matches for review."
            )
        result = sync(import_report, trakt_token())
        print(json.dumps(result, indent=2))
    else:
        print(f"Review {report_path.name}, then rerun with --commit to import exact matches.")


if __name__ == "__main__":
    main()
