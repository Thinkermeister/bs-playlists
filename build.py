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
  6. practice.txt の設定に従い、ScoreSaber の自分の記録と突き合わせて
     「基準精度に届いていない譜面だけ」の練習リストを生成（未プレイも対象に含める）
  7. Quest のブラウザから使う一覧ページ docs/index.html を生成

ローカル実行:  python build.py   （GITHUB_REPOSITORY 未設定時は DEFAULT_REPO を使う）
"""
import html as html_mod
import json
import os
import re
import sys
import time
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
PRACTICE_FILE = ROOT / "practice.txt"
SS_SCORES_URL = "https://scoresaber.com/api/player/{pid}/scores?limit=100&sort=recent&page={page}"
SS_DIFF_NAMES = {1: "Easy", 3: "Normal", 5: "Hard", 7: "Expert", 9: "ExpertPlus"}
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


# ---------- 練習リスト ----------

def read_practice() -> list[tuple[str, list[str], float, str]]:
    """practice.txt を読む。列: 出力名 / 対象リスト(カンマ区切り) / 基準精度 / 中央ラベル"""
    if not PRACTICE_FILE.exists():
        return []
    items = []
    for line in PRACTICE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            print(f"  無視（書式不正）: {line}")
            continue
        name, srcs, thr, label = parts[0], parts[1].split(","), float(parts[2]), parts[3]
        items.append((name, srcs, thr, label))
    return items


def fetch_my_scores(pid: str) -> dict[tuple[str, str, str], float]:
    """ScoreSaber から自分の全スコアを取得。(hash, characteristic, 難易度名) -> 精度%"""
    acc: dict[tuple[str, str, str], float] = {}
    page = 1
    while page <= 100:                                    # 暴走防止
        data = json.loads(fetch(SS_SCORES_URL.format(pid=pid, page=page)).decode("utf-8"))
        items = data.get("playerScores") or []
        if not items:
            break
        for it in items:
            lb, sc = it.get("leaderboard") or {}, it.get("score") or {}
            h = (lb.get("songHash") or "").strip().lower()
            diff = lb.get("difficulty") or {}
            dname = SS_DIFF_NAMES.get(diff.get("difficulty"))
            mode = diff.get("gameMode") or "SoloStandard"
            chara = mode[4:] if mode.startswith("Solo") else mode
            maxscore, base = lb.get("maxScore") or 0, sc.get("baseScore") or 0
            if not (h and dname and maxscore):
                continue
            a = base / maxscore * 100
            key = (h, chara, dname)
            if a > acc.get(key, -1.0):
                acc[key] = a
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.3)
    print(f"ScoreSaber の自己記録: {len(acc)}譜面 ({page}ページ)")
    return acc


def make_practice_cover(src_name: str, label: str, thr: float) -> str | None:
    """元リストのカバーを流用し、色を変えて基準精度を書き込む。失敗したら None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import base64, io
        src = OUT_DIR / f"{src_name}.bplist"
        d = json.loads(src.read_text(encoding="utf-8"))
        raw = d.get("imageString") or d.get("image") or ""
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        im = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
        im = im.resize((256, 256), Image.NEAREST)
        r, g, b = im.split()
        im = Image.merge("RGB", (b, g, r))                # 黄 → シアン（練習リストの目印）

        def font(sz):
            for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"):
                try:
                    return ImageFont.truetype(p, sz)
                except Exception:
                    pass
            return ImageFont.load_default()

        dr = ImageDraw.Draw(im)
        W, H = im.size
        if "-" in label:                                   # 範囲指定なら元の数字を隠して書き直す
            dr.rectangle([W * 0.14, H * 0.20, W * 0.86, H * 0.72], fill=(26, 222, 255))
            f = font(int(W * 0.30))
            bb = dr.textbbox((0, 0), label, font=f)
            dr.text(((W - (bb[2] - bb[0])) / 2 - bb[0], H * 0.46 - (bb[3] - bb[1]) / 2 - bb[1]),
                    label, font=f, fill=(0, 0, 0))
        bh = int(H * 0.24)
        dr.rectangle([0, H - bh, W, H], fill=(20, 20, 20))
        f2 = font(int(bh * 0.62))
        t2 = f"\u2264{thr:g}%"
        bb = dr.textbbox((0, 0), t2, font=f2)
        dr.text(((W - (bb[2] - bb[0])) / 2 - bb[0], H - bh + (bh - (bb[3] - bb[1])) / 2 - bb[1]),
                t2, font=f2, fill=(0, 221, 255))
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        print(f"  カバー生成スキップ ({src_name}): {e}", file=sys.stderr)
        return None


def build_practice(scores: dict[tuple[str, str, str], float]) -> int:
    """基準精度に届いていない譜面だけを集めた練習リストを作る。未プレイは対象に含める。"""
    ok = 0
    for name, srcs, thr, label in read_practice():
        merged: dict[str, dict] = {}
        order: list[str] = []
        total = 0
        for src in srcs:
            f = OUT_DIR / f"{src}.bplist"
            if not f.exists():
                print(f"  {name}: 元リスト {src} が無いのでスキップ", file=sys.stderr)
                continue
            for song in json.loads(f.read_text(encoding="utf-8")).get("songs", []):
                h = song["hash"]
                total += len(song.get("difficulties", []))
                keep = [d for d in song.get("difficulties", [])
                        if scores.get((h, d.get("characteristic"), d.get("name")), -1.0) <= thr]
                if not keep:
                    continue                              # 全難易度が基準を超えていれば曲ごと除外
                if h in merged:
                    ex = {(d.get("characteristic"), d.get("name")) for d in merged[h]["difficulties"]}
                    for d in keep:
                        if (d.get("characteristic"), d.get("name")) not in ex:
                            merged[h]["difficulties"].append(d)
                else:
                    s2 = dict(song)
                    s2["difficulties"] = keep
                    merged[h] = s2
                    order.append(h)
        if not order:
            print(f"  {name}: 残る譜面なし（全部達成済み）")
            continue
        data = {
            "playlistTitle": f"\u7df4\u7fd2 \u2605{label} {thr:g}%\u4ee5\u4e0b",
            "playlistAuthor": "bs-playlists",
            "songs": [merged[h] for h in order],
            "customData": {"syncURL": raw_url(name)},
        }
        cover = make_practice_cover(srcs[0], label, thr)
        if cover:
            data["imageString"] = cover
        kept = sum(len(s["difficulties"]) for s in data["songs"])
        if save(name, data, len(data["songs"]), "practice"):
            ok += 1
            print(f"    {name}: {kept}/{total}譜面が基準以下")
    return ok


# ---------- 一覧ページ ----------

def build_index() -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    groups: dict[str, list[Path]] = {"練習リスト": [], "JBSL（開催中）": [], "ScoreSaber": [],
                                     "BeatLeader": [], "その他": []}
    for f in sorted(OUT_DIR.glob("*.bplist")):
        if f.stem.startswith("practice_"):
            groups["練習リスト"].append(f)
        elif f.stem.startswith("jbsl_"):
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
    # 練習リスト（ScoreSaber の自己記録と突き合わせ）
    pid = os.environ.get("SCORESABER_ID", "").strip()
    if not pid:
        print("SCORESABER_ID が未設定のため練習リストはスキップ（既存ファイルは維持）")
    elif read_practice():
        try:
            scores = fetch_my_scores(pid)
            if not scores:
                raise ValueError("スコアを1件も取得できなかった")
            ok += build_practice(scores)
        except Exception as e:
            print(f"練習リストの生成をスキップ（既存ファイルを維持）: {e}", file=sys.stderr)
    build_index()
    print(f"完了: 成功 {ok} / 失敗 {fail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
