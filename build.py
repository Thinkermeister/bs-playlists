#!/usr/bin/env python3
"""
Beat Saber プレイリスト配布リポジトリのビルドスクリプト。

毎回やること:
  1. sources.txt のプレイリスト（ScoreSaber / BeatLeader）を取得
  2. JBSL の leagues ページから開催中の大会プレイリストを自動発見して取得
  3. すべてに共通変換: hash小文字化・levelid付与・同一曲の難易度統合・
     customData.syncURL をこのリポジトリの raw URL に書き換え
  4. 検証してから playlists/ に保存（失敗時は既存ファイルを維持）
  5. 開催が終わった JBSL リストを archive/ へ移動
  6. Quest のブラウザから使う一覧ページ docs/index.html を生成

ローカル実行:  python build.py   （GITHUB_REPOSITORY 未設定時は DEFAULT_REPO を使う）
"""
import html as html_mod
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_REPO = "Thinkermeister/bs-playlists"
BRANCH = "main"
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "playlists"
ARCHIVE_DIR = ROOT / "archive"
DOCS_DIR = ROOT / "docs"
SOURCES = ROOT / "sources.txt"
JBSL_LEAGUES_URL = "https://jbsl-web.herokuapp.com/leagues/"
# 固定枠: 出力名 -> 開催中リーグのタイトルにこの正規表現がマッチしたら、その中身を入れる。
# ファイル名と同期URLが変わらないので、Quest側は一度入れれば以後は同期ボタンだけで
# 最新の大会内容に入れ替わる（旅行先で新しい大会が始まっても追加作業が不要）。
JBSL_SLOTS = {
    "jbsl_current_div4": r"Div\.\s*4",
}
JBSL_DL_URL = "https://jbsl-web.herokuapp.com/download_playlist/{}"
TIMEOUT = 60
UA = "bs-playlists-proxy/1.0 (+https://github.com/%s)"
JST = timezone(timedelta(hours=9))


def repo_name() -> str:
    return os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)


def raw_url(name: str) -> str:
    return f"https://raw.githubusercontent.com/{repo_name()}/{BRANCH}/playlists/{name}.bplist"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA % repo_name()})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


# ---------- 共通変換 ----------

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
    cd["syncURL"] = raw_url(name)
    data["customData"] = cd
    return data


def save(name: str, data: dict, before: int, label: str) -> bool:
    dst = OUT_DIR / f"{name}.bplist"
    try:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        check = json.loads(text)
        if not check.get("songs"):
            raise ValueError("songs が空")
        if dst.exists() and dst.read_text(encoding="utf-8") == text:
            print(f"変更なし: {name} ({len(check['songs'])}曲)")
        else:
            dst.write_text(text, encoding="utf-8")
            print(f"更新: {name} [{label}] 元{before}曲 → {len(check['songs'])}曲")
        return True
    except Exception as e:
        kept = "（既存ファイルを維持）" if dst.exists() else "（ファイルなし）"
        print(f"失敗: {name} {kept} {e}", file=sys.stderr)
        return False


# ---------- sources.txt ----------

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


# ---------- JBSL 自動発見 ----------

def slugify(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^0-9a-z]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def discover_jbsl() -> tuple[list[tuple[str, dict, int]], bool]:
    """開催中リーグのプレイリストを取得して (name, data, 元曲数) を返す。
    2番目の戻り値は leagues ページ取得に成功したか（アーカイブ判断に使う）。"""
    try:
        page = fetch(JBSL_LEAGUES_URL).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"JBSL leaguesページ取得失敗（今回は発見もアーカイブもスキップ）: {e}", file=sys.stderr)
        return [], False
    live = page.split("End Leagues")[0]
    ids = list(dict.fromkeys(re.findall(r"download_playlist/(\d+)", live)))
    print(f"JBSL 開催中プレイリスト: {len(ids)}件 {ids}")
    results = []
    used_names: set[str] = set()
    for pid in ids:
        try:
            data = json.loads(fetch(JBSL_DL_URL.format(pid)).decode("utf-8-sig"))
            before = len(data.get("songs", []))
            slug = slugify(str(data.get("playlistTitle") or pid))
            if len(slug) < 4:          # 日本語のみのタイトル等で識別子が残らない場合はIDを使う
                slug = pid
            name = "jbsl_" + slug
            if name in used_names:
                name = f"{name}_{pid}"
            used_names.add(name)
            results.append((name, normalize(data, name), before))
        except Exception as e:
            print(f"JBSL id={pid} 取得失敗: {e}", file=sys.stderr)
    return results, True


def fill_slots(jbsl: list[tuple[str, dict, int]]) -> list[tuple[str, dict, int]]:
    """開催中リーグの中から、固定枠のパターンに合うものを枠名でも書き出す。"""
    out = []
    for slot, pattern in JBSL_SLOTS.items():
        hit = None
        for _, data, before in jbsl:
            title = str(data.get("playlistTitle") or "")
            if re.search(pattern, title):
                hit = (data, before, title)
                break
        if hit is None:
            print(f"固定枠 {slot}: 該当する開催中リーグなし（既存ファイルを維持）")
            continue
        data, before, title = hit
        copy = json.loads(json.dumps(data))       # 元データを壊さないよう複製
        copy["playlistTitle"] = f"{title} [current]"
        copy["customData"] = dict(copy.get("customData") or {})
        copy["customData"]["syncURL"] = raw_url(slot)
        out.append((slot, copy, before))
        print(f"固定枠 {slot}: 「{title}」を割り当て")
    return out


def archive_ended(live_names: set[str]) -> None:
    ARCHIVE_DIR.mkdir(exist_ok=True)
    for f in sorted(OUT_DIR.glob("jbsl_*.bplist")):
        if f.stem in JBSL_SLOTS:
            continue                              # 固定枠は常に残す
        if f.stem not in live_names:
            dst = ARCHIVE_DIR / f.name
            if dst.exists():
                dst.unlink()
            f.rename(dst)
            print(f"アーカイブ: {f.name}（開催終了）")


# ---------- 一覧ページ ----------

def build_index() -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    groups: dict[str, list[Path]] = {"JBSL（開催中）": [], "ScoreSaber": [], "BeatLeader": [], "その他": []}
    for f in sorted(OUT_DIR.glob("*.bplist")):
        if f.stem.startswith("jbsl_"):
            groups["JBSL（開催中）"].append(f)
        elif f.stem.startswith("bl_"):
            groups["BeatLeader"].append(f)
        elif f.stem.startswith("ranked_"):
            groups["ScoreSaber"].append(f)
        else:
            groups["その他"].append(f)
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>bs-playlists</title>",
        "<style>body{font-family:sans-serif;background:#14141f;color:#eee;margin:1em;}",
        "h2{border-bottom:1px solid #555;padding-bottom:4px;}",
        "li{margin:6px 0;} a{color:#8cf;} .dl{margin-left:0.8em;font-size:0.9em;}",
        "p.note{color:#aaa;font-size:0.9em;}</style></head><body>",
        f"<h1>bs-playlists</h1><p class='note'>最終生成: {now}。",
        "「インストール」はワンクリック対応環境用、効かない場合は「DL」で保存してください。</p>",
    ]
    for group, files in groups.items():
        if not files:
            continue
        parts.append(f"<h2>{html_mod.escape(group)}</h2><ul>")
        for f in files:
            try:
                title = json.loads(f.read_text(encoding="utf-8")).get("playlistTitle") or f.stem
            except Exception:
                title = f.stem
            url = raw_url(f.stem)
            parts.append(
                f"<li><a href='bsplaylist://playlist/{url}'>{html_mod.escape(str(title))}</a>"
                f"<a class='dl' href='{url}' download>[DL]</a></li>"
            )
        parts.append("</ul>")
    parts.append("</body></html>")
    (DOCS_DIR / "index.html").write_text("\n".join(parts), encoding="utf-8")
    print("一覧ページ生成: docs/index.html")


# ---------- main ----------

def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    ok = fail = 0
    # 定常ソース
    for name, url in read_sources():
        try:
            data = json.loads(fetch(url).decode("utf-8-sig"))
            before = len(data.get("songs", []))
            if save(name, normalize(data, name), before, "sources"):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            dst = OUT_DIR / f"{name}.bplist"
            kept = "（既存ファイルを維持）" if dst.exists() else "（ファイルなし）"
            print(f"失敗: {name} {kept} {e}", file=sys.stderr)
    # JBSL
    jbsl, discovered = discover_jbsl()
    for name, data, before in jbsl:
        if save(name, data, before, "jbsl"):
            ok += 1
        else:
            fail += 1
    for name, data, before in fill_slots(jbsl):
        if save(name, data, before, "jbsl-slot"):
            ok += 1
        else:
            fail += 1
    if discovered:
        archive_ended({name for name, _, _ in jbsl})
    build_index()
    print(f"完了: 成功 {ok} / 失敗 {fail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
