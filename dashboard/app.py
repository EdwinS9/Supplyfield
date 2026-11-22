"""Vercel entry point: the control room served from the committed snapshot in dashboard/snapshot."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gtl.api.snapshot import hosted_app  # noqa: E402

app = hosted_app(REPO, REPO / "dashboard" / "snapshot", Path("/tmp/control-room"), live_translate=True)
