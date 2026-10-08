"""Away days: during the listed hours a built resume is also copied to iCloud Drive.
`python -m pytest tests/test_away.py` from the project root."""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tailor  # noqa: E402

pytestmark = pytest.mark.unit

AWAY = """hours: "09:00-17:00"
folder: JobResumes
dates:
  - "2026-10-08"
  - 2026-10-09
  - "2026-10-14 12:00-18:00"
"""


@pytest.fixture
def away(tmp_path, monkeypatch):
    path = tmp_path / "away.yaml"
    path.write_text(AWAY)
    monkeypatch.setattr(tailor, "AWAY_FILE", str(path))
    monkeypatch.setattr(tailor, "ICLOUD_ROOT", str(tmp_path / "icloud"))
    return tmp_path


@pytest.fixture
def resume(tmp_path):
    folder = tmp_path / "applications" / "Acme__Software_Engineer__42"
    folder.mkdir(parents=True)
    docx = folder / "KALYANKUMAR_KONDURU_ACME.docx"
    docx.write_bytes(b"resume")
    return str(docx), str(folder)


@pytest.mark.parametrize("when, expected", [
    ("2026-10-08 09:00", True),
    ("2026-10-08 16:59", True),
    ("2026-10-08 08:59", False),
    ("2026-10-08 17:00", False),
    ("2026-10-09 12:00", True),          # unquoted YAML date still counts
    ("2026-10-10 12:00", False),         # not listed
    ("2026-10-14 10:00", False),         # own hours start at 12:00
    ("2026-10-14 17:30", True),          # ... and run to 18:00
])
def test_away_window(away, when, expected):
    assert tailor.away_now(datetime.strptime(when, "%Y-%m-%d %H:%M")) is expected


def test_no_file_means_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tailor, "AWAY_FILE", str(tmp_path / "missing.yaml"))
    assert tailor.away_now(datetime(2026, 10, 8, 10)) is False


def test_copies_resume_during_away_hours(away, resume):
    docx, folder = resume
    dest = tailor.copy_to_icloud(docx, folder, now=datetime(2026, 10, 8, 10))
    assert dest == str(away / "icloud" / "JobResumes" / "Acme__Software_Engineer__42"
                       / "KALYANKUMAR_KONDURU_ACME.docx")
    assert open(dest, "rb").read() == b"resume"
    assert os.path.exists(docx)                     # local copy kept


def test_no_copy_when_home(away, resume):
    docx, folder = resume
    assert tailor.copy_to_icloud(docx, folder, now=datetime(2026, 10, 8, 18)) is None
    assert not (away / "icloud").exists()


def test_failed_copy_does_not_raise(away, resume, monkeypatch):
    docx, folder = resume

    def boom(*a, **k):
        raise PermissionError("no iCloud access")
    monkeypatch.setattr(tailor.shutil, "copy2", boom)
    assert tailor.copy_to_icloud(docx, folder, now=datetime(2026, 10, 8, 10)) is None


def test_discord_card_shows_phone_path(away, monkeypatch):
    sent = {}

    class Ok:
        status_code = 204
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(tailor.requests, "post",
                        lambda url, json, timeout: sent.update(json) or Ok())
    dest = os.path.join(tailor.ICLOUD_ROOT, "JobResumes", "Acme__SE__42", "R.docx")
    tailor.notify_discord({"title": "SE", "url": "https://x"},
                          {"recruiter": {"score": 80}, "icloud": dest}, "/local/Acme__SE__42")
    fields = {f["name"]: f["value"] for f in sent["embeds"][0]["fields"]}
    assert fields["📱 On your phone"] == \
        "Files › iCloud Drive › JobResumes › Acme__SE__42 › R.docx"
