#!/usr/bin/env python3
"""构建供 MaiBot WebUI 直接上传的发行 ZIP；供本地或 GitHub Actions 使用。"""
from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
# 构建时拦截无效 manifest，避免打完包才在 WebUI 发现缺字段。
from urllib.parse import urlsplit
import re

required_fields = {
    "manifest_version", "id", "version", "name", "description",
    "author", "license", "urls", "host_application", "sdk",
    "capabilities", "i18n",
}
missing_fields = required_fields.difference(manifest)
if missing_fields:
    raise ValueError(f"Manifest 缺少必填字段: {sorted(missing_fields)}")
if manifest["manifest_version"] != 2:
    raise ValueError("Manifest 协议必须为 2")
if not isinstance(manifest["author"], dict) or not all(manifest["author"].get(k) for k in ("name", "url")):
    raise ValueError("Manifest author 必须有 name 和 url")
if not isinstance(manifest["urls"], dict) or not manifest["urls"].get("repository"):
    raise ValueError("Manifest urls 必须有 repository")
for name, value in [("author.url", manifest["author"]["url"]), *[(f"urls.{key}", value) for key, value in manifest["urls"].items()]]:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{name} 必须是 HTTP/HTTPS URL")
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(manifest["version"])):
    raise ValueError("Manifest version 必须是 X.Y.Z")

version = manifest["version"]
required = (
    "__init__.py", "_manifest.json", "plugin.py", "policy.py", "explore.py",
    "bilibili.py", "store.py", "mysql_store.py", "owner.py", "relationship.py", "README.md", "LICENSE",
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
