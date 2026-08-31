#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bplist の各曲の zip の「中身の構造」を調べる。

PlaylistManager 側の展開処理（BeatSaverPlusPlus / Utils::ExtractAll）は、
zip 内のエントリ名をそのまま使ってファイルを書き出す。つまり zip が

    MapFolder/Info.dat
    MapFolder/song.egg

のように一段深い構造だと、

    CustomLevels/<key> (曲名 - マッパー)/MapFolder/Info.dat

に展開される。SongCore は曲フォルダ直下の Info.dat を探すので、この曲は
「ダウンロードは成功しているのにレベルとして認識されない」状態になる。
ゲーム内の曲ダウンローダーで落とすと入る、という現象と整合する。

zip 全体は落とさず、HTTP Range で末尾の中央ディレクトリだけを読む。
1曲あたり数十KBで済むので、387曲でも数分で終わる。

使い方:
    python check_zip_layout.py playlists/practice_bl_04.bplist
"""

import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile

API = "https://api.beatsaver.com/maps/hash/"
UA = "playlist-zip-layout-check/1.0 (personal diagnostic)"
BATCH = 50
SLEEP = 0.5


class HTTPRangeFile(io.RawIOBase):
    """HTTP Range で必要な部分だけ読む、seek 可能なファイル風オブジェクト。"""

    def __init__(self, url, size):
        self.url = url
        self.size = size
        self.pos = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n == 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + n, self.size) - 1
        req = urllib.request.Request(
            self.url,
            headers={"User-Agent": UA, "Range": f"bytes={self.pos}-{end}"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        self.pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def http_size(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            return int(cr.rsplit("/", 1)[1])
        return int(r.headers.get("Content-Length", 0))


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


def inspect(url):
    """zip のエントリ一覧を取得して構造を判定する。"""
    size = http_size(url)
    f = HTTPRangeFile(url, size)
    with zipfile.ZipFile(io.BufferedReader(f, buffer_size=65536)) as z:
        names = z.namelist()

    root_files = [n for n in names if "/" not in n.strip("/") and "\\" not in n]
    nested = [n for n in names if ("/" in n.strip("/")) or ("\\" in n)]
    has_root_info = any(n.lower() == "info.dat" for n in root_files)
    nonascii = [n for n in names if any(ord(c) > 127 for c in n)]

    if has_root_info:
        verdict = "OK"
    elif any(n.lower().endswith("info.dat") for n in names):
        verdict = "NESTED_INFO"   # Info.dat が一段以上深い
    else:
        verdict = "NO_INFO"       # Info.dat が見当たらない

    return {
        "entries": len(names),
        "verdict": verdict,
        "nested_count": len(nested),
        "nonascii_count": len(nonascii),
        "sample": " | ".join(names[:6]),
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    data = load_playlist(sys.argv[1])
    songs = data.get("songs", [])
    hashes = [s["hash"].lower() for s in songs]
    name_of = {s["hash"].lower(): f"{s.get('songName','')} / {s.get('levelAuthorName','')}" for s in songs}
    print(f"playlist: {data.get('playlistTitle')}  songs: {len(songs)}")

    # ダウンロードURLを取得（mod と同じ経路）
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
        print(f"  照会 {min(i+BATCH, len(hashes))}/{len(hashes)}")
        time.sleep(SLEEP)

    rows = []
    flagged = []
    for n, h in enumerate(hashes, 1):
        key, url = url_of.get(h, ("", ""))
        row = {"hash": h, "song": name_of.get(h, ""), "beatsaver_id": key, "zip_url": url}
        if not url:
            row.update({"verdict": "NO_URL", "entries": "", "nested_count": "", "nonascii_count": "", "sample": ""})
        else:
            try:
                row.update(inspect(url))
            except Exception as e:
                row.update({"verdict": f"ERROR: {type(e).__name__}", "entries": "", "nested_count": "", "nonascii_count": "", "sample": ""})
        rows.append(row)
        if row["verdict"] != "OK":
            flagged.append(row)
        if n % 50 == 0:
            print(f"  zip確認 {n}/{len(hashes)}")
        time.sleep(0.1)

    out = "zip_layout_check.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print()
    print(f"直下に Info.dat が無い / 読めない zip: {len(flagged)} / {len(rows)}")
    for r in flagged:
        print(f"  [{r['verdict']}] {r['beatsaver_id']}  {r['song']}")
        if r["sample"]:
            print(f"      中身: {r['sample']}")
    print()
    print(f"詳細: {out}")


if __name__ == "__main__":
    main()
