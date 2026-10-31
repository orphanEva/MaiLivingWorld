"""回归：六库迁移程序必须支持 GitHub ZIP 的目录名，不依赖固定包名文件夹。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MigrationLauncherTests(unittest.TestCase):
    def test_preview_runs_from_arbitrarily_named_source_directories(self):
        for folder in ("MaiLivingWorld-main", "my-custom-directory", "MaiLivingWorld"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / folder
                source.mkdir()
                scripts = source / "scripts"
                scripts.mkdir()
                for file in ROOT.glob("*.py"):
                    shutil.copy2(file, source / file.name)
                shutil.copy2(ROOT / "scripts" / "migrate_all_sqlite_to_mysql.py",
                             scripts / "migrate_all_sqlite_to_mysql.py")
                backup = Path(temp) / "backups"
                backup.mkdir()
                result = subprocess.run(
                    [sys.executable, str(scripts / "migrate_all_sqlite_to_mysql.py"),
                     "--directory", str(backup)],
                    cwd=source,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("DRY RUN", result.stdout)
                self.assertIn("缺失文件", result.stdout)


if __name__ == "__main__":
    unittest.main()
