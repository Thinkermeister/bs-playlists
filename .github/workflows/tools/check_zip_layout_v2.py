#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bplist の各曲の zip の「中身の構造」を調べる（v2: zip を丸ごと取得する方式）。

v1 は HTTP Range で末尾だけ読んでいたが、CDN 側の応答が安定せず
387曲中243曲が解析エラーになった（曲の並びと無関係にバラけていたので、
中身ではなく取得方法の問題）。v2 は zip を丸ごと落としてから解析する。
ディスクには書かずメモリ上で開いて捨てるので、容量は消費しない。

判定の目的:
    PlaylistManager 側の展開処理（BeatSaverPlusPlus / Utils::ExtractAll）は
    zip 内のエントリ名をそのまま出力先に連結する。zip が

        MapFolder/Info.dat

    のように一段深いと

        CustomLevels/<key> (曲名 - マッパー)/MapFolder/Info.dat

    に展開され、SongCore が Info.dat を見つけられず永久に認識されない。
    その形の zip を探す。

使い方:
    python check_zip_layout.py playlists/practice_bl_04.bplist
"""

import csv
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

API = "https://api.beatsaver.com/maps/hash/"
UA = "playlist-zip-layout-check/2.0 (personal diagnostic)"
BATCH = 50
SLEEP = 0.5
WORKERS = 4

_print_lock = threading.Lock()
_done = 0


def load_playlist(src):
    if src.startswith("http"):
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


def fetch(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read(), None
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (i + 1))
    return None, last


def classify(names):
    root = [n for n in names if "/" not in n.strip("/") and "\\" not in n]
    nested = [n for n in names if ("/" in n.strip("/")) or ("\\" in n)]
    has_root_info = any(n.lower() == "info.dat" for n in root)
    any_info = any(n.lower().endswith("info.dat") for n in names)

    if has_root_info:
        verdict = "OK"
    elif any_info:
        verdict = "NESTED_INFO"   # Info.dat が一段以上深い＝本命
    else:
        verdict = "NO_INFO"       # Info.dat が見当たらない

    return {
        "verdict": verdict,
        "entries": len(names),
        "nested_count": len(nested),
        "nonascii_count": sum(1 for n in names if any(ord(c) > 127 for c in n)),
        "backslash_count": sum(1 for n in names if "\\" in n),
        "sample": " | ".join(names[:8]),
    }


def work(item, total):
    global _done
    h, song, key, url = item
    row = {"hash": h, "song": song, "beatsaver_id": key, "zip_url": url,
           "verdict": "", "entries": "", "nested_count": "", "nonascii_count": "",
           "backslash_count": "", "size": "", "sample": "", "error": ""}

    if not url:
        row["verdict"] = "NO_URL"
    else:
        data, err = fetch(url)
        if data is None:
            row["verdict"] = "FETCH_FAIL"
            row["error"] = err or ""
        else:
            row["size"] = len(data)
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    names = z.namelist()
                row.update(classify(names))
            except Exception as e:
                row["verdict"] = "PARSE_FAIL"
                row["error"] = f"{type(e).__name__}: {e}"

    with _print_lock:
        _done += 1
        if _done % 25 == 0 or _done == total:
            print(f"  zip確認 {_done}/{total}", flush=True)
    return row


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    data = load_playlist(sys.argv[1])
    songs = data.get("songs", [])
    hashes = [s["hash"].lower() for s in songs]
    name_of = {s["hash"].lower(): f"{s.get('songName','')} / {s.get('levelAuthorName','')}" for s in songs}
    print(f"playlist: {data.get('playlistTitle')}  songs: {len(songs)}")

    url_of = {}
    for i in range(0, len(hashes), BATCH):
        chunk = hashes[i:i + BATCH]
        res = get_json(API + ",".join(chunk)) or {}
        lowered = {k.lower(): v for k, v in res.items()} if isinstance(res, dict) else {}
        for h in chunk:
            m = lowered.get(h)
            if not m:
                continue
            for v in m.get("versions", []):
                if (v.get("hash") or "").lower() == h:
                    url_of[h] = (m.get("id"), v.get("downloadURL"))
                    break
        print(f"  照会 {min(i+BATCH, len(hashes))}/{len(hashes)}", flush=True)
        time.sleep(SLEEP)

    items = []
    for h in hashes:
        key, url = url_of.get(h, ("", ""))
        items.append((h, name_of.get(h, ""), key, url))

    total = len(items)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        rows = list(ex.map(lambda it: work(it, total), items))

    out = "zip_layout_check.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print()
    print("判定内訳:", counts)
    print()

    flagged = [r for r in rows if r["verdict"] not in ("OK",)]
    print(f"直下に Info.dat が無い / 解析できない zip: {len(flagged)} / {len(rows)}")
    for r in flagged:
        print(f"  [{r['verdict']}] {r['beatsaver_id']}  {r['song']}")
        if r["sample"]:
            print(f"      中身: {r['sample']}")
        if r["error"]:
            print(f"      err : {r['error']}")
    print()
    print(f"詳細: {out}")


if __name__ == "__main__":
    main()
