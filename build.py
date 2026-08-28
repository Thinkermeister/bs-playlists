#!/usr/bin/env python3
"""
sources.txt に書かれたプレイリストを取得し、Quest版 PlaylistManager 1.3系で
難易度ハイライトが正しく効く形に変換して playlists/ に保存する。

変換内容:
  1. hash を小文字化し、levelid を custom_level_<小文字hash> にする
  2. 同じ hash の重複エントリを 1 つに統合し、difficulties を合算する
  3. customData.syncURL を このリポジトリの raw URL に書き換える
  4. JSON として読み直し、曲が 1 つ以上あることを確認してから保存する
取得や検証に失敗した場合は、既存の playlists/<名前>.bplist をそのまま残す。

ローカル実行:  python build.py            （GITHUB_REPOSITORY 未設定時は下の DEFAULT_REPO を使う）
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

DEFAULT_REPO = "Thinkermeister/bs-playlists"
BRANCH = "main"
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "playlists"
SOURCES = ROOT / "sources.txt"
TIMEOUT = 60
UA = "bs-playlists-proxy/1.0 (+https://github.com/%s)"


def repo_name() -> str:
    return os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)


def sync_url(name: str) -> str:
    return f"https://raw.githubusercontent.com/{repo_name()}/{BRANCH}/playlists/{name}.bplist"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA % repo_name()})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def normalize(data: dict, name: str) -> dict:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for song in data.get("songs", []):
        h = song.get("hash")
        if not isinstance(h, str) or not h.strip():
            continue  # hash のないエントリは Quest で扱えないので捨てる
        h = h.strip().lower()
        song["hash"] = h
        song["levelid"] = f"custom_level_{h}"
        diffs = song.get("difficulties") or []
        if h in merged:
            base = merged[h]
            existing = {(d.get("characteristic"), d.get("name")) for d in base.get("difficulties", [])}
            for d in diffs:
                key = (d.get("characteristic"), d.get("name"))
                if key not in existing:
                    base.setdefault("difficulties", []).append(d)
                    existing.add(key)
        else:
            # 同一エントリ内の重複難易度も除去
            seen = set()
            uniq = []
            for d in diffs:
                key = (d.get("characteristic"), d.get("name"))
                if key not in seen:
                    seen.add(key)
                    uniq.append(d)
            song["difficulties"] = uniq
            merged[h] = song
            order.append(h)
    data["songs"] = [merged[h] for h in order]
    cd = data.get("customData")
    if not isinstance(cd, dict):
        cd = {}
    cd["syncURL"] = sync_url(name)
    data["customData"] = cd
    return data


def read_sources() -> list[tuple[str, str]]:
    items = []
    for line in SOURCES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            print(f"  無視（書式不正）: {line}")
            continue
        items.append((parts[0], parts[1].strip()))
    return items


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    ok = fail = 0
    for name, url in read_sources():
        dst = OUT_DIR / f"{name}.bplist"
        try:
            raw = fetch(url)
            data = json.loads(raw.decode("utf-8-sig"))
            before = len(data.get("songs", []))
            data = normalize(data, name)
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            check = json.loads(text)
            if not check.get("songs"):
                raise ValueError("songs が空")
            if dst.exists() and dst.read_text(encoding="utf-8") == text:
                print(f"変更なし: {name} ({len(check['songs'])}曲)")
            else:
                dst.write_text(text, encoding="utf-8")
                print(f"更新: {name} 元{before}曲 → {len(check['songs'])}曲")
            ok += 1
        except Exception as e:
            fail += 1
            kept = "（既存ファイルを維持）" if dst.exists() else "（ファイルなし）"
            print(f"失敗: {name} {kept} {e}", file=sys.stderr)
    print(f"完了: 成功 {ok} / 失敗 {fail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
