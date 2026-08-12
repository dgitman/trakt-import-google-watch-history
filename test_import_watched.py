import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import import_watched


class ExtractJsonTests(unittest.TestCase):
    def test_extracts_watched_records_and_deduplicates_exact_copies(self):
        records = [
            {"Published": "2022-06-04T00:57:24Z", "Search Query": "Blade Runner"},
            {"Published": "2022-06-04T00:57:24Z", "Search Query": "Blade Runner"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "takeout.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Takeout/Search Contributions/Watched.json", json.dumps(records))
            self.assertEqual(
                import_watched.extract_json([archive_path]),
                [{"title": "Blade Runner", "watched_at": "2022-06-04T00:57:24Z"}],
            )

    def test_ignores_damaged_timestamp_when_correct_copy_exists(self):
        records = [
            {"Published": "1970-01-20T20:41:19Z", "Search Query": "Mother of the Bride"},
            {"Published": "2024-05-19T00:46:26Z", "Search Query": "Mother of the Bride"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "takeout.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Takeout/Search Contributions/Watched.json", json.dumps(records))
            self.assertEqual(
                import_watched.extract_json([archive_path]),
                [{"title": "Mother of the Bride", "watched_at": "2024-05-19T00:46:26Z"}],
            )

    def test_extracts_google_tv_activity_and_excludes_ads_and_generic_media(self):
        activity = """<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
        <div>Watched&nbsp;Last Action Hero<br>Aug 1, 2026, 9:59:59&#8239;AM EDT<br></div></div>
        <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
        <div>Watched&nbsp;Media<br>Aug 2, 2026, 1:00:00&#8239;PM EDT<br></div></div>
        <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
        <div>Watched&nbsp;1998<br>Aug 2, 2026, 1:30:00&#8239;PM EDT<br></div></div>
        <div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">
        <div>Watched&nbsp;Some Ad<br>Aug 3, 2026, 1:00:00&#8239;PM EDT<br></div>
        <div><b>Details:</b><br>From Google Ads<br></div></div>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "MyActivity.html"
            path.write_text(activity)
            self.assertEqual(
                import_watched.extract_google_tv_activity(path),
                [{"title": "Last Action Hero", "watched_at": "2026-08-01T13:59:59Z"}],
            )


if __name__ == "__main__":
    unittest.main()
