# -*- coding: utf-8 -*-
"""
GitHub API 上传脚本（通用版，网络限制时的替代通道）
====================================================
git push 连不上 github.com 主站时，用此脚本经 api.github.com 推送。
通过 Git Database API 构造提交，能保留完整提交历史。

与 spider_framework 旧版差异：
- 文件清单自动扫描（排除 .git/ backups/ data/ 等），新增文件无需手动加入
- 仓库名通过命令行参数指定，默认 stock-forecast

用法: python3 tools/upload_github.py "提交说明" [owner/repo]
"""
import base64
import json
import re
import sys
from pathlib import Path

import requests

OWNER_DEFAULT = "Hypnotized-zbc"
REPO_DEFAULT = "stock-forecast"
BRANCH = "main"

# 自动扫描时排除的目录 / 扩展名
EXCLUDE_DIRS = {".git", "backups", "data", "__pycache__", "output", "node_modules"}
EXCLUDE_EXT = {".pyc", ".png", ".jpg", ".pdf"}

ROOT = Path(__file__).resolve().parent.parent


def get_token() -> str:
    cred = (Path.home() / ".git-credentials").read_text(encoding="utf-8")
    m = re.search(r"//[^:]+:([^@]+)@github\.com", cred)
    if not m:
        sys.exit("未找到 GitHub token（~/.git-credentials）")
    return m.group(1)


def api(session, method: str, url: str, **kwargs):
    r = session.request(method, url, **kwargs)
    r.raise_for_status()
    return r.json() if r.text else {}


def scan_files() -> list:
    """扫描项目目录下所有应上传的文件（相对路径，排序保证稳定）。"""
    files = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix in EXCLUDE_EXT:
            continue
        files.append(rel)
    return files


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "update"
    repo_arg = sys.argv[2] if len(sys.argv) > 2 else f"{OWNER_DEFAULT}/{REPO_DEFAULT}"
    owner, repo = repo_arg.split("/") if "/" in repo_arg else (OWNER_DEFAULT, repo_arg)
    API = f"https://api.github.com/repos/{owner}/{repo}"

    token = get_token()
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json"}
    s = requests.Session()
    s.headers.update(headers)

    # 1. 当前分支 HEAD
    head = None
    try:
        ref = api(s, "GET", f"{API}/git/ref/heads/{BRANCH}")
        head = ref["object"]["sha"]
    except requests.HTTPError:
        pass  # 空仓库，首次提交

    # 2. 读取本地文件并创建 blob
    files = scan_files()
    blobs = {}
    for rel in files:
        p = ROOT / rel
        content = base64.b64encode(p.read_bytes()).decode()
        blob = api(s, "POST", f"{API}/git/blobs",
                   json={"content": content, "encoding": "base64"})
        blobs[rel] = blob["sha"]

    # 3. 构造树：保留远程已有条目，更新/新增本地文件
    base_tree = None
    tree_items = []
    if head:
        tree = api(s, "GET", f"{API}/git/trees/{head}?recursive=1")
        base_tree = tree["sha"]
        for entry in tree["tree"]:
            if entry["type"] == "tree" or entry["path"] in blobs:
                continue  # 目录或将被替换的文件
            tree_items.append({"path": entry["path"], "mode": entry["mode"],
                               "type": "blob", "sha": entry["sha"]})
    for rel, sha in blobs.items():
        tree_items.append({"path": rel, "mode": "100644",
                           "type": "blob", "sha": sha})

    new_tree = api(s, "POST", f"{API}/git/trees",
                   json={"base_tree": base_tree, "tree": tree_items})["sha"]

    # 4. 创建提交
    commit = api(s, "POST", f"{API}/git/commits",
                 json={"message": msg, "tree": new_tree,
                       "parents": [head] if head else []})
    print(f"commit {commit['sha'][:7]} {msg}")

    # 5. 更新分支引用
    if head:
        api(s, "PATCH", f"{API}/git/refs/heads/{BRANCH}",
            json={"sha": commit["sha"], "force": False})
    else:
        api(s, "POST", f"{API}/git/refs",
            json={"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    print(f"已推送 {len(blobs)} 个文件 -> {owner}/{repo} ({BRANCH})")


if __name__ == "__main__":
    main()
