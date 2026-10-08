import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tailor  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_away_days(tmp_path, monkeypatch):
    """The real away.yaml must never send a test resume to iCloud Drive."""
    monkeypatch.setattr(tailor, "AWAY_FILE", str(tmp_path / "no-away.yaml"))
