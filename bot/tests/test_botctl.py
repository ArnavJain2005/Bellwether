"""Offline operator launcher checks; no systemd service, network or SIG key."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "deploy/botctl.sh"


class BotctlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / "app"
        (self.app / ".venv/bin").mkdir(parents=True)
        (self.app / ".venv/bin/python").symlink_to(sys.executable)
        (self.app / "bot/state").mkdir(parents=True)
        (self.app / "config").mkdir()
        (self.app / "bot/__init__.py").write_text("")
        (self.app / "bot/limits.py").write_text(
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "def load_limits(path=Path('config/risk.yaml')):\n"
            "    return SimpleNamespace(mode=Path(path).read_text().strip(), loop_seconds=10)\n"
        )
        (self.app / "bot/mapping.py").write_text("def load_map():\n    return ['approved']\n")
        self.config = self.app / "config/risk.yaml"
        self.config.write_text("dry_run")
        self.script = self.root / "botctl.sh"
        self.script.write_text(
            SCRIPT.read_text().replace("APP=/opt/bellwether", f"APP={self.app}", 1)
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "systemctl"
        fake.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            " is-active) test -e \"$FAKE_ACTIVE\" || exit 3; "
            "test \"$2\" = '--quiet' || echo active ;;\n"
            " is-enabled) test -e \"$FAKE_ENABLED\" && echo enabled ;;\n"
            " *) exit 99 ;;\n"
            "esac\n"
        )
        fake.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text(
            "#!/bin/sh\n"
            "test \"$*\" = 'systemctl enable --now bellwether-bot.service' || exit 99\n"
            "touch \"$FAKE_ACTIVE\" \"$FAKE_ENABLED\"\n"
        )
        sudo.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        FAKE_ACTIVE=str(self.root / "active"),
                        FAKE_ENABLED=str(self.root / "enabled"))

    def run_ctl(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.script), command], cwd=self.app,
                              env=self.env, capture_output=True, text=True, timeout=5, check=False)

    def heartbeat(self, *, age: int = 0, paused: int = 0, mode: str = "dry_run") -> None:
        db = self.app / "bot/state/bellwether.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE heartbeat (id INT, ts_utc TEXT, mode TEXT, "
                         "paused INT, pause_reason TEXT)")
            ts = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
            conn.execute("INSERT INTO heartbeat VALUES (1, ?, ?, ?, '')", (ts, mode, paused))

    def test_start_enables_service_after_dry_run_preflight(self) -> None:
        self.heartbeat()
        result = self.run_ctl("start")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.root / "active").exists())
        self.assertIn("Heartbeat:", result.stdout)

    def test_start_preserves_stop_and_pause(self) -> None:
        for filename in ("STOP", "PAUSED"):
            path = self.app / "bot/state" / filename
            path.touch()
            result = self.run_ctl("start")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(path.exists())
            self.assertFalse((self.root / "active").exists())
            path.unlink()

    def test_start_rejects_live_config(self) -> None:
        self.config.write_text("live")
        result = self.run_ctl("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "active").exists())

    def test_status_does_not_report_stale_as_healthy(self) -> None:
        self.heartbeat(age=120)
        (self.root / "active").touch()
        (self.root / "enabled").touch()
        result = self.run_ctl("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stdout)


if __name__ == "__main__":
    unittest.main()
