import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import launcher  # noqa: E402
import updater  # noqa: E402


def make_archive(path, version, extra=None, app="print('hi')\n"):
    files = {"app.py": app, "updater.py": "x = 1\n", "VERSION": version + "\n", "static/icon-192.png": "png"}
    files.update(extra or {})
    with tarfile.open(path, "w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "data" / "app"
        self.legacy = self.tmp / "camera"
        self.legacy.mkdir()
        for f, t in (("app.py", ""), ("updater.py", ""), ("VERSION", "1.0.0\n")):
            (self.legacy / f).write_text(t)
        self.tmp.joinpath("data").mkdir()
        for mod in (launcher, updater):
            p = mock.patch.object(mod, "ROOT", self.root)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(launcher, "HERE", self.legacy)
        p.start()
        self.addCleanup(p.stop)
        os.environ["LEGACY_DIR"] = str(self.legacy)

    def install(self, version, **kw):
        return updater.install_file(make_archive(self.tmp / ("r%s.tgz" % version), version, **kw))

    def choose(self):
        d, slot, reason = launcher.choose()
        return d.name if slot != "legacy" else "legacy", slot, reason


class LauncherTest(Base):
    def test_no_data_partition_runs_legacy(self):
        self.assertEqual(self.choose(), ("legacy", "legacy", ""))

    def test_installed_release_is_started(self):
        self.assertEqual(self.install("1.1.0"), "1.1.0")
        self.assertEqual(self.choose()[:2], ("1.1.0", "current"))

    def test_failing_release_falls_back_to_previous_then_legacy(self):
        self.install("1.1.0")
        self.install("1.2.0")
        for _ in range(launcher.MAX):
            self.assertEqual(self.choose()[:2], ("1.2.0", "current"))
        name, slot, reason = self.choose()
        self.assertEqual((name, slot), ("1.1.0", "previous"))
        self.assertIn("1.2.0", reason)
        self.assertEqual(updater.status()["current"], "1.1.0")
        for _ in range(launcher.MAX - 1):
            self.assertEqual(self.choose()[1], "current")
        self.assertEqual(self.choose()[:2], ("legacy", "legacy"))
        self.assertEqual(self.choose()[:2], ("legacy", "legacy"))  # stays there

    def test_mark_good_resets_attempts(self):
        self.install("1.1.0")
        for _ in range(10):
            self.assertEqual(self.choose()[1], "current")
            updater.mark_good("1.1.0")

    def test_incomplete_release_runs_legacy(self):
        self.install("1.1.0")
        (self.root / "releases" / "1.1.0" / "app.py").unlink()
        name, slot, reason = self.choose()
        self.assertEqual(slot, "legacy")
        self.assertTrue(reason)

    def test_new_install_clears_failure(self):
        self.install("1.1.0")
        for _ in range(launcher.MAX + 1):
            self.choose()
        self.install("1.1.1")
        self.assertEqual(self.choose()[:2], ("1.1.1", "current"))
        self.assertEqual(updater.status()["fallback"], "")


class UpdaterTest(Base):
    def test_rollback_and_legacy(self):
        self.install("1.1.0")
        self.install("1.2.0")
        self.assertEqual(updater.rollback(), "1.1.0")
        s = updater.status()
        self.assertEqual((s["current"], s["previous"]), ("1.1.0", "1.2.0"))
        self.assertEqual(updater.use_legacy(), "1.0.0")
        self.assertEqual(self.choose()[:2], ("legacy", "legacy"))
        self.assertEqual(updater.rollback(), "1.1.0")

    def test_only_two_releases_kept(self):
        for v in ("1.1.0", "1.2.0", "1.3.0"):
            self.install(v)
        self.assertEqual(sorted(p.name for p in (self.root / "releases").iterdir()), ["1.2.0", "1.3.0"])

    def test_bad_archives_change_nothing(self):
        self.install("1.1.0")
        bad = [
            dict(extra={"../evil": "x"}),
            dict(app="def broken(:\n"),
        ]
        for kw in bad:
            with self.assertRaises(updater.UpdateError):
                self.install("1.2.0", **kw)
        with self.assertRaises(updater.UpdateError):
            self.install("1.1.0")  # already installed
        self.assertEqual(updater.status()["current"], "1.1.0")
        self.assertEqual([p.name for p in (self.root / "releases").iterdir()], ["1.1.0"])

    def test_install_latest_verifies_checksum(self):
        arc = make_archive(self.tmp / "a.tgz", "2.0.0")
        good = updater.sha256(arc)
        name = "target-cam-2.0.0.tar.gz"
        rel = {"version": "2.0.0", "notes": "", "assets": {name: {"url": "A"}, name + ".sha256": {"url": "S"}}}

        def fake_download(sha):
            def dl(url, dest, accept=None):
                Path(dest).write_bytes(arc.read_bytes() if url == "A" else (sha + "  " + name).encode())
            return dl

        with mock.patch.object(updater, "latest_release", return_value=rel):
            with mock.patch.object(updater, "download", fake_download("0" * 64)):
                with self.assertRaises(updater.UpdateError):
                    updater.install_latest()
            with mock.patch.object(updater, "download", fake_download(good)):
                self.assertEqual(updater.install_latest(), "2.0.0")
        self.assertEqual(updater.status()["current"], "2.0.0")

    def test_check_records_result(self):
        self.root.mkdir(parents=True)
        with mock.patch.object(updater, "latest_release", return_value={"version": "9.0.0", "notes": "n", "assets": {}}):
            s = updater.check()
        self.assertTrue(s["update_available"] is False or s["latest"] == "9.0.0")
        self.assertEqual(json.loads((self.root / "state" / "update.json").read_text())["latest"], "9.0.0")
        with mock.patch.object(updater, "latest_release", side_effect=updater.UpdateError("offline")):
            with self.assertRaises(updater.UpdateError):
                updater.check()
        st = updater.status()
        self.assertEqual((st["latest"], st["check_error"]), ("9.0.0", "offline"))

    def test_versions(self):
        self.assertGreater(updater.parse_version("v1.10.0"), updater.parse_version("1.9.9"))
        self.assertIsNone(updater.parse_version("1.0"))


if __name__ == "__main__":
    unittest.main()
