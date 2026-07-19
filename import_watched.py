#!/usr/bin/env python3
"""Import Google Search/Google TV watched marks into Trakt history."""

from __future__ import annotations

import argparse
import datetime as dt
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


def request_json(url: str, *, method="GET", headers=None, body=None):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
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
        return json.loads(path.read_text())["access_token"]
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


def match(rows: list[dict]) -> dict:
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
    (ROOT / "report.json").write_text(json.dumps(report, indent=2))
    return report


def sync(report: dict, token: str) -> dict:
    headers = trakt_headers(token)
    def all_watched(kind):
        items, page = [], 1
        while True:
            batch = request_json(
                f"https://api.trakt.tv/users/me/watched/{kind}?extended=noseasons&limit=250&page={page}",
                headers=headers,
            )
            items.extend(batch)
            if len(batch) < 250:
                return items
            page += 1

    watched_movies = all_watched("movies")
    watched_shows = all_watched("shows")
    existing_movies = {item["movie"]["ids"]["trakt"] for item in watched_movies}
    existing_shows = {item["show"]["ids"]["trakt"] for item in watched_shows}
    pending = {"movies": {}, "shows": {}}
    for item in report["matched"]:
        kind = "movies" if item["type"] == "movie" else "shows"
        trakt_id = item["ids"]["trakt"]
        if trakt_id in (existing_movies if kind == "movies" else existing_shows):
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
    totals["skipped_existing"] = {"movies": len(existing_movies), "shows": len(existing_shows)}
    return totals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--takeout", type=Path,
                        help="Google Takeout archive or extracted directory; skips Data Portability OAuth")
    parser.add_argument("--commit", action="store_true", help="Write reviewed matches to Trakt")
    args = parser.parse_args()
    load_env(ROOT / ".env.google")
    load_env(ROOT / ".env.trakt")
    required = ["TRAKT_CLIENT_ID", "TRAKT_CLIENT_SECRET"]
    if not args.takeout:
        required += ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"]
    absent = [name for name in required if not os.environ.get(name)]
    if absent: raise SystemExit("Missing variables: " + ", ".join(absent))
    if args.takeout:
        takeout = args.takeout.expanduser().resolve()
        if not takeout.exists():
            raise SystemExit(f"Takeout path does not exist: {takeout}")
        downloads = [takeout]
    else:
        google = google_token()
        downloads = google_archive(google["access_token"])
    if not downloads: return
    rows = extract_json(downloads)
    print(f"Found {len(rows)} Google watched records")
    report = match(rows)
    print(f"Exact: {len(report['matched'])}; ambiguous: {len(report['ambiguous'])}; missing: {len(report['missing'])}")
    if args.commit:
        result = sync(report, trakt_token())
        print(json.dumps(result, indent=2))
    else:
        print("Review report.json, then rerun with --commit to import exact matches.")


if __name__ == "__main__":
    main()
