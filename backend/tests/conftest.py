"""Tests run against a throwaway SQLite database and never call the AI model."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="leadpilot-tests-")
os.environ.update({
    "DATABASE_URL": "", "ENV": "development", "SQLITE_PATH": str(Path(_tmp) / "test.db"),
    "ADMIN_TOKENS": "skyline:sky-test-key,autohub:auto-test-key,*:root-test-key",
    "SESSION_SECRET": "test-secret", "CRON_SECRET": "cron-test", "ANTHROPIC_API_KEY": "sk-ant-test-not-real",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
