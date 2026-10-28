#!/usr/bin/env python3
"""构建供 MaiBot WebUI 直接上传的发行 ZIP；供本地或 GitHub Actions 使用。"""
from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
version = manifest["version"]
required = (
    "__init__.py", "_manifest.json", "plugin.py", "policy.py", "explore.py",
    "bilibili.py", "store.py", "mysql_store.py", "owner.py", "README.md", "LICENSE",
)
for name in required:
    if not (ROOT / name).is_file():
        raise FileNotFoundError(f"发布插件缺少 {name}")
OUTPUT = ROOT / "dist" / f"MaiLivingWorld-v{version}-WebUI.zip"
OUTPUT.parent.mkdir(exist_ok=True)
with ZipFile(OUTPUT, "w", ZIP_DEFLATED, compresslevel=9) as archive:
    for name in required:
        archive.write(ROOT / name, arcname=f"MaiLivingWorld/{name}")
with ZipFile(OUTPUT) as archive:
    paths = set(archive.namelist())
    assert "MaiLivingWorld/_manifest.json" in paths
    assert "MaiLivingWorld/plugin.py" in paths
    assert archive.testzip() is None
print(OUTPUT)
