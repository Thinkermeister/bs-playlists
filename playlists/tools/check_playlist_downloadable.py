#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bplist の各曲が、Quest版 PlaylistManager と同じ手順で実際に落とせるかを確認する。

mod 側の実際の動作（Metalit/PlaylistCore + bsq-ports/BeatSaverPlusPlus）:
  1. 不足ハッシュを50個ずつ  https://api.beatsaver.com/maps/hash/h1,h2,...  に投げる
  2. 応答の versions から version.hash == 要求ハッシュ の版を探し、その downloadURL を落とす
  3. 見つからなければ  https://cdn.beatsaver.com/{hash}.zip  を決め打ちで試す
  4. 失敗しても記録せず次へ進む（＝毎回同じ曲で同じように失敗する）

このスクリプトは 1〜3 を再現し、どの曲が「原理的に落とせない」のかを判定する。

使い方:
    python check_playlist_downloadable.py practice_bl_04.bplist
    python check_playlist_downloadable.py https://raw.githubusercontent.com/.../practice_bl_04.bplist

出力:
    - 標準出力に判定サマリ
    - <入力名>_check.csv に全曲の判定結果
"""

import csv
import json
import sys
import time
import urllib.error
import urllib.request

API = "https://api.beatsaver.com/maps/hash/"
CDN = "https://cdn.beatsaver.com/{}.zip"
UA = "playlist-download-check/1.0 (personal diagnostic)"
BATCH = 50
SLEEP = 0.5  # BeatSaver への負荷を避ける


def load_playlist(src):
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": UA})) as r:
            return json.loads(r.read().decode("utf-8"))
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def get_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code == 429:
                time.sleep(5 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def head_ok(url, retries=2):
    """CDN に zip が実在するか。存在すれば (True, サイズ)。"""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                return True, r.headers.get("Content-Length", "")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return False, str(e.code)
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return False, "error"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = sys.argv[1]
    data = load_playlist(src)
    songs = data.get("songs", [])
    print(f"playlist: {data.get('playlistTitle')}  songs: {len(songs)}")

    hashes = [s["hash"].lower() for s in songs]
    name_of = {s["hash"].lower(): f"{s.get('songName','')} / {s.get('levelAuthorName','')}" for s in songs}

    # --- 手順1: ハッシュ一括照会 ---
    found = {}  # hash -> (map_id, version_hash_matched(bool), version_state, downloadURL)
    for i in range(0, len(hashes), BATCH):
        chunk = hashes[i:i + BATCH]
        res = get_json(API + ",".join(chunk))
        if res is None:
            print(f"  [warn] batch {i}-{i+len(chunk)} 照会失敗（ネットワーク or レート制限）")
            res = {}
        # 応答はハッシュ小文字キーの辞書
        lowered = {k.lower(): v for k, v in res.items()} if isinstance(res, dict) else {}
        for h in chunk:
            m = lowered.get(h)
            if not m:
                continue
            match = None
            for v in m.get("versions", []):
                if (v.get("hash") or "").lower() == h:
                    match = v
                    break
            found[h] = {
                "id": m.get("id"),
                "matched": match is not None,
                "state": (match or {}).get("state", ""),
                "url": (match or {}).get("downloadURL", ""),
                "latest": ((m.get("versions") or [{}])[0].get("hash") or "").lower(),
            }
        print(f"  照会 {min(i+BATCH, len(hashes))}/{len(hashes)}")
        time.sleep(SLEEP)

    # --- 手順2/3: 実際に落とせるかを zip の有無で確認 ---
    rows = []
    problems = []
    for h in hashes:
        info = found.get(h)
        if info and info["matched"] and info["url"]:
            url = info["url"]
            verdict_hint = "版あり"
        else:
            url = CDN.format(h)  # mod のフォールバックと同じ
            verdict_hint = "マップ無し" if not info else "版が見つからない"
        ok, detail = head_ok(url)
        verdict = "OK" if ok else "NG"
        if not ok:
            problems.append((h, name_of.get(h, ""), verdict_hint, detail))
        rows.append({
            "hash": h,
            "song": name_of.get(h, ""),
            "beatsaver_id": (info or {}).get("id", ""),
            "map_found": bool(info),
            "version_matched": (info or {}).get("matched", False),
            "state": (info or {}).get("state", ""),
            "latest_hash": (info or {}).get("latest", ""),
            "zip_url": url,
            "zip_available": ok,
            "detail": detail,
            "verdict": verdict,
        })
        time.sleep(0.2)

    out = (src.rsplit("/", 1)[-1].rsplit(".", 1)[0]) + "_check.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print()
    print(f"落とせない曲: {len(problems)} / {len(hashes)}")
    for h, n, hint, detail in problems:
        print(f"  [{hint}/{detail}] {h}  {n}")
    print()
    print(f"詳細: {out}")


if __name__ == "__main__":
    main()
