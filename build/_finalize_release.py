# -*- coding: utf-8 -*-
"""收尾：附件改名 hanhua-tool.exe；更新 Release 说明；清理临时脚本。"""
import json
import subprocess
import urllib.request

TOKEN = subprocess.run([r"C:\Program Files\GitHub CLI\gh.exe", "auth", "token"],
                       capture_output=True, encoding="utf-8").stdout.strip()
HDR = {"Authorization": "Bearer " + TOKEN,
       "Accept": "application/vnd.github+json",
       "User-Agent": "release-helper"}


def api(path, data=None, method=None):
    req = urllib.request.Request("https://api.github.com" + path,
                                 data=data, method=method, headers=HDR)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return json.loads(body) if body else None


# 1) 附件改名（ASCII 才保得住）
print("asset:", api("/repos/keyminori/hanhua-tool/releases/assets/581403458",
                    data=b'{"name": "hanhua-tool.exe"}',
                    method="PATCH")["name"])

# 2) Release 说明补充文件名说明
rel = api("/repos/keyminori/hanhua-tool/releases/tags/v1.0.1")
body = rel["body"]
if "hanhua-tool.exe" not in body:
    body = body + "\n\n---\n📎 附件名英文（GitHub 会剥掉附件名里的中文）：`hanhua-tool.exe` 即「汉化工具」，下载后可随意改名。"
api("/repos/keyminori/hanhua-tool/releases/" + str(rel["id"]),
    data=json.dumps({"body": body}).encode("utf-8"), method="PATCH")
print("release body updated")

# 3) 清理临时脚本
import os
for f in ("_rename_asset.py", "_upload_asset.py"):
    p = r"J:\hanhua\build" + "\\" + f
    if os.path.isfile(p):
        os.remove(p)
print("temp cleaned")
