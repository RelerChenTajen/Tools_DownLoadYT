#!/usr/bin/env python3
"""
fill_tags.py - 用「演唱者 + 曲名」搜尋 YouTube Music，補齊 MP3 的專輯、發行年份與正方形專輯封面

只改 ID3 標籤，不重新編碼，音質與音量不變。

分兩階段：
  1. 掃描：搜尋結果寫進 CSV 報告（不動 MP3）。每次最多處理 --batch 首，可重複執行接續，已在報告中的不重查。
  2. 寫入：--apply 依報告中 apply=Y 的列寫入（不重新搜尋，看到的就是寫入的）。
報告可用 Excel 開啟檢查，把不要寫入的列 apply 改成 N。

用法:
    python fill_tags.py                                   # 掃描 output/（預設），寫報告
    python fill_tags.py "E:\\Music\\某資料夾" --batch 30    # 掃描指定資料夾，每次 30 首
    python fill_tags.py --apply                           # 依報告寫入
    python fill_tags.py --tolerance 30                    # 長度差容許秒數（預設 60）
    python fill_tags.py --keep-cover                      # 不換封面
    python fill_tags.py --all                             # 標籤已齊全的檔案也重新查
"""
import argparse
import csv
import glob
import os
import re
import sys
import time
import unicodedata
import urllib.request

try:
    from ytmusicapi import YTMusic
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1
    from mutagen.mp3 import MP3
except ImportError:
    sys.exit("缺少套件，請先執行: python -m pip install -U ytmusicapi mutagen")

DEFAULT_OUTDIR = "output"
DEFAULT_TOLERANCE = 60       # 秒；只用來排除明顯不同的版本（MV 與專輯版常差數十秒）
COVER_SIZE = 1200            # 專輯封面邊長（px）；YouTube Music 圖片網址可指定尺寸
MAX_CANDIDATES = 6           # 每首最多比較幾個候選（每個候選要多查一次專輯）
MAX_DISCOGRAPHY = 8          # 往演唱者專輯清單找更早原版時，最多檢查幾張
DEFAULT_REPORT = "fill_tags_report.csv"
DEFAULT_BATCH = 30           # 每次執行最多新掃描幾首
DEFAULT_DELAY = 1.0          # 每首之間暫停秒數，避免 YouTube Music 限速

REPORT_FIELDS = ["apply", "status", "file", "note", "cur_artist", "cur_title", "new_artist", "new_title",
                 "album", "year", "type", "file_dur", "track_dur", "url", "cover_url", "applied"]

# 候選標題含這些字，但原曲名沒有時，視為不同版本
_VERSION_WORDS = re.compile(
    r"\b(live|karaoke|cover|remix|instrumental|acoustic|demo|edit|mix|version|tribute|8d|sped|slowed|"
    r"re-?recorded|re recorded)\b",
    re.IGNORECASE,
)
_BRACKETS = re.compile(r"\s*[\(\[【（].*?[\)\]】）]")


def norm(s: str) -> str:
    """比對用：去括號內容、全半形統一、去標點、轉小寫。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("⧸", "/").replace("&", " and ")
    s = _BRACKETS.sub(" ", s)
    s = re.sub(r"[^\w]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def split_artists(s: str) -> list[str]:
    parts = re.split(r"\s*(?:/|,|;|&|\bfeat\.?|\bft\.?|\band\b|\bx\b)\s*", unicodedata.normalize("NFKC", s or ""),
                     flags=re.IGNORECASE)
    return [p for p in (norm(x) for x in parts) if p]


def artist_match(mine: str, theirs: list[str]) -> bool:
    a = set(split_artists(mine)) | {norm(mine)}
    b = {norm(x) for x in theirs} | {p for x in theirs for p in split_artists(x)}
    return bool(a & b)


def title_match(mine: str, theirs: str) -> bool:
    if norm(mine) != norm(theirs):
        return False
    # 括號內的版本字樣（例如 "(Live 1993)"）：原曲名沒有就排除
    return not (_VERSION_WORDS.search(theirs) and not _VERSION_WORDS.search(mine))


def read_current(path: str) -> dict:
    """讀出目前的演唱者、曲名、專輯、年份、長度；沒有標籤就從「演唱者 - 曲名」檔名拆。"""
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    get = lambda k: str(tags[k].text[0]) if k in tags and tags[k].text else ""
    artist, title = get("TPE1"), get("TIT2")
    tagged = bool(artist and title)
    if not tagged:
        stem = os.path.splitext(os.path.basename(path))[0].replace("⧸", "/")
        # 「演唱者 - 曲名」，也接受「演唱者- 曲名」（但不拆 A-ha 這種名字內的連字號）
        m = re.match(r"^(.+?)\s+-\s+(.+)$", stem) or re.match(r"^(.+?)-\s+(.+)$", stem)
        if m:
            artist, title = artist or m.group(1).strip(), title or m.group(2).strip()
    cur = {
        "artist": artist, "title": title, "tagged": tagged, "album": get("TALB"), "year": get("TDRC")[:4],
        "duration": MP3(path).info.length,
        "has_cover": any(k.startswith("APIC") for k in tags.keys()),
    }
    cur["complete"] = tagged and bool(cur["album"] and cur["year"] and cur["has_cover"])
    return cur


def cover_url(thumbnails: list[dict]) -> str | None:
    if not thumbnails:
        return None
    url = max(thumbnails, key=lambda t: t.get("width", 0))["url"]
    # 網址尾端形如 =w544-h544-...，改成想要的尺寸
    return re.sub(r"=w\d+-h\d+", f"=w{COVER_SIZE}-h{COVER_SIZE}", url)


def find_match(yt: YTMusic, cur: dict, tolerance: float) -> tuple[dict | None, str]:
    """回傳 (最佳候選, 說明)。候選要演唱者、曲名相符且長度差在容許內；多個時取發行年份最早者。
    之後再到演唱者專輯清單找更早收錄同名曲的專輯。

    名稱相符但長度都超出容許時，取長度最接近的一個，並在說明加註請使用者確認。
    """
    named = []
    # 搜尋結果每次略有不同；第一組關鍵字找不到再換順序試一次
    for query in (f"{cur['artist']} {cur['title']}", f"{cur['title']} {cur['artist']}"):
        named = [r for r in yt.search(query, filter="songs", limit=20)
                 if r.get("album") and r.get("duration_seconds")
                 and artist_match(cur["artist"], [a["name"] for a in r.get("artists") or []])
                 and title_match(cur["title"], r["title"])]
        if named:
            break
    if not named:
        return None, "找不到相符的音軌"
    cands = [r for r in named if abs(r["duration_seconds"] - cur["duration"]) <= tolerance][:MAX_CANDIDATES]
    warn = ""
    if not cands:
        cands = [min(named, key=lambda r: abs(r["duration_seconds"] - cur["duration"]))]
        warn = f"，⚠ 長度差 {abs(cands[0]['duration_seconds'] - cur['duration']):.0f}s 超過 {tolerance:g}s，請確認"

    best = None
    for order, r in enumerate(cands):
        album = yt.get_album(r["album"]["id"])
        year = str(album.get("year") or "")
        cand = {
            "video_id": r["videoId"], "title": r["title"],
            "artists": [a["name"] for a in r["artists"]],
            "album": album.get("title") or r["album"]["name"], "year": year,
            "type": album.get("type") or "", "duration": r["duration_seconds"],
            "cover": cover_url(album.get("thumbnails")),
        }
        # 年份早者優先（原版優於精選輯），同年時 Album 優先於 Single，再依長度接近、搜尋排名
        key = (int(year) if year.isdigit() else 9999, cand["type"] != "Album",
               abs(cand["duration"] - cur["duration"]), order)
        if best is None or key < best[0]:
            best = (key, cand)
    best, note = best[1], f"{len(cands)} 個候選{warn}"

    # 搜尋結果不一定含原版專輯：再從演唱者專輯清單找更早、且收錄同名曲的專輯
    earlier = find_in_discography(yt, cur, best, first_artist_id(cands))
    if earlier:
        best, note = earlier, f"{len(cands)} 個候選，改用演唱者專輯清單中更早的版本"
        diff = abs(best["duration"] - cur["duration"])
        if diff > tolerance:
            note += f"，⚠ 長度差 {diff:.0f}s 超過 {tolerance:g}s，請確認"
    return best, note


def first_artist_id(cands: list[dict]) -> str | None:
    for r in cands:
        for a in r.get("artists") or []:
            if a.get("id"):
                return a["id"]
    return None


def find_in_discography(yt: YTMusic, cur: dict, best: dict, artist_id: str | None) -> dict | None:
    """在演唱者自己的專輯中找更早收錄同名曲者；不限長度（MV 與專輯版常不同），由呼叫端加註。"""
    if not artist_id or not best["year"].isdigit():
        return None
    section = yt.get_artist(artist_id).get("albums") or {}
    albums = section.get("results") or []
    if section.get("browseId"):
        albums = yt.get_artist_albums(section["browseId"], section.get("params")) or albums
    older = sorted((a for a in albums if str(a.get("year") or "").isdigit() and int(a["year"]) < int(best["year"])),
                   key=lambda a: int(a["year"]))
    for a in older[:MAX_DISCOGRAPHY]:
        album = yt.get_album(a["browseId"])
        for t in album.get("tracks") or []:
            dur = t.get("duration_seconds")
            if t.get("videoId") and dur and title_match(cur["title"], t.get("title") or ""):
                return {
                    "video_id": t["videoId"], "title": t["title"],
                    "artists": [x["name"] for x in t.get("artists") or []] or best["artists"],
                    "album": album.get("title") or a.get("title"), "year": str(album.get("year") or a["year"]),
                    "type": album.get("type") or "", "duration": dur,
                    "cover": cover_url(album.get("thumbnails")),
                }
    return None


def write_tags(path: str, row: dict, cover: bytes | None):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TALB", [TALB(encoding=3, text=row["album"])])
    if row["year"]:
        tags.setall("TDRC", [TDRC(encoding=3, text=row["year"])])
    # 原本沒有演唱者/曲名才寫入（採 YouTube Music 正式寫法），不覆蓋既有值
    if "TPE1" not in tags:
        tags.add(TPE1(encoding=3, text=row["new_artist"]))
    if "TIT2" not in tags:
        tags.add(TIT2(encoding=3, text=row["new_title"]))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(path, v2_version=3)


def load_report(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_report(path: str, rows: list[dict]):
    # utf-8-sig：Excel 才會正確顯示中文；先寫暫存檔再取代，中斷也不會毀損報告
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def collect_files(paths: list[str]) -> list[str]:
    files = []
    for p in paths or [DEFAULT_OUTDIR]:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "*.mp3")))
        elif p.lower().endswith(".mp3"):
            files.append(p)
    return [os.path.abspath(f) for f in files]


def scan_one(yt: YTMusic, path: str, tolerance: float) -> dict:
    cur = read_current(path)
    row = {"file": path, "cur_artist": cur["artist"], "cur_title": cur["title"],
           "file_dur": f"{cur['duration']:.0f}", "applied": ""}
    if not (cur["artist"] and cur["title"]):
        return {**row, "apply": "N", "status": "skip", "note": "無法判斷演唱者/曲名"}
    m, note = find_match(yt, cur, tolerance)
    if not m:
        return {**row, "apply": "N", "status": "notfound", "note": note}
    if _COMPILATION.search(m["album"]):
        note += "，⚠ 可能是精選輯／合輯，年份可能不是原始發行年"
    warn = "⚠" in note
    return {
        **row, "apply": "N" if warn else "Y", "status": "warn" if warn else "ok", "note": note,
        # 原本有標籤就沿用；沒有才採 YouTube Music 的正式寫法
        "new_artist": cur["artist"] if cur["tagged"] else pick_artist(cur["artist"], m["artists"]),
        "new_title": cur["title"] if cur["tagged"] else tidy_title(m["title"], cur["title"]),
        "album": m["album"], "year": m["year"], "type": m["type"], "track_dur": str(m["duration"]),
        "url": f"https://music.youtube.com/watch?v={m['video_id']}", "cover_url": m["cover"] or "",
    }


_COMPILATION = re.compile(
    r"\b(greatest|best of|hits|collection|classics|essential|anthology|definitive|love songs|vol\.?|volume|"
    r"magic of|made in california|valentine)\b",
    re.IGNORECASE,
)


def pick_artist(file_artist: str, yt_artists: list[str]) -> str:
    """採 YouTube Music 寫法；但檔名列出的合唱者比 YouTube Music 多時（漏列合唱者），改用檔名並以 & 連接。"""
    if len(split_artists(file_artist)) > len(yt_artists):
        return re.sub(r"\s*,\s*", " & ", file_artist.strip())
    return " & ".join(yt_artists)


def tidy_title(yt_title: str, file_title: str) -> str:
    """去掉 YouTube Music 曲名中、原曲名沒有的括號說明，例如 (From "Dirty Dancing" Soundtrack)、(Remastered)。"""
    plain = lambda s: re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", s).lower()).strip()

    def keep(m):
        return m.group(0) if plain(m.group(0)) and plain(m.group(0)) in plain(file_title) else ""
    t = re.sub(r"\s*[\(\[【（][^\(\)\[\]【】（）]*[\)\]】）]", keep, yt_title).strip()
    return re.sub(r"\s+([?!])$", r"\1", t) or yt_title


def print_row(row: dict):
    mark = {"ok": "✓", "warn": "⚠"}.get(row["status"], "✗")
    print(f"{mark} {os.path.basename(row['file'])}  （{row['note']}）")
    if row["status"] in ("ok", "warn"):
        print(f"    {row['cur_artist']} - {row['cur_title']}  →  {row['new_artist']} - {row['new_title']}"
              f"  [{row['track_dur']}s，檔案 {row['file_dur']}s]")
        print(f"    專輯: {row['album']} ({row['type']})   年份: {row['year'] or '（無）'}   {row['url']}")


def cmd_scan(args) -> int:
    rows = load_report(args.report)
    done = {r["file"] for r in rows}
    files = collect_files(args.paths)
    todo = []
    for f in files:
        if f in done:
            continue
        if not args.all and read_current(f)["complete"]:
            continue
        todo.append(f)
    print(f"共 {len(files)} 首；報告已有 {len(done & set(files))} 首；待掃描 {len(todo)} 首，本次處理 {min(len(todo), args.batch)} 首\n")

    yt = YTMusic()
    for i, f in enumerate(todo[:args.batch], 1):
        try:
            row = scan_one(yt, f, args.tolerance)
        except Exception as e:  # 網路或限速：不寫進報告，下次重試
            print(f"✗ {os.path.basename(f)}  （搜尋失敗，下次重試：{e}）")
            continue
        rows.append(row)
        save_report(args.report, rows)      # 每首存一次，中斷可接續
        print(f"[{i}] ", end="")
        print_row(row)
        if i < min(len(todo), args.batch):
            time.sleep(args.delay)

    remaining = len(todo) - min(len(todo), args.batch)
    stat = {s: sum(r["status"] == s for r in rows if r["file"] in set(files)) for s in ("ok", "warn", "notfound", "skip")}
    print(f"\n報告：{os.path.abspath(args.report)}")
    print(f"相符 {stat['ok']}、需確認 {stat['warn']}（預設 apply=N）、找不到 {stat['notfound']}、略過 {stat['skip']}；"
          f"尚待掃描 {remaining} 首")
    print("再執行一次可繼續下一批；檢查報告後用 --apply 寫入" if remaining else "檢查報告後用 --apply 寫入")
    return 0


def cmd_apply(args) -> int:
    rows = load_report(args.report)
    if not rows:
        print(f"沒有報告 {args.report}，請先執行掃描")
        return 1
    targets = [r for r in rows if r["apply"].strip().upper() == "Y" and r["status"] in ("ok", "warn") and not r["applied"]]
    print(f"依報告寫入 {len(targets)} 首\n")
    ok = 0
    for r in targets:
        name = os.path.basename(r["file"])
        try:
            cover = None
            if not args.keep_cover and r["cover_url"]:
                with urllib.request.urlopen(r["cover_url"], timeout=30) as resp:
                    cover = resp.read()
            write_tags(r["file"], r, cover)
            r["applied"] = time.strftime("%Y-%m-%d %H:%M")
            ok += 1
            print(f"✓ {name}  →  {r['new_artist']} - {r['new_title']} / {r['album']} ({r['year']})")
        except Exception as e:
            print(f"✗ {name}  寫入失敗：{e}")
        save_report(args.report, rows)
    print(f"\n已寫入 {ok} / {len(targets)} 首")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="用 YouTube Music 補齊 MP3 的演唱者、曲名、專輯、年份、封面")
    ap.add_argument("paths", nargs="*", help=f"MP3 檔或資料夾（預設 {DEFAULT_OUTDIR}/）")
    ap.add_argument("--apply", action="store_true", help="依報告寫入 apply=Y 的列")
    ap.add_argument("--report", default=DEFAULT_REPORT, help=f"報告 CSV 路徑（預設 {DEFAULT_REPORT}）")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH, help=f"每次最多新掃描幾首（預設 {DEFAULT_BATCH}）")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"每首間隔秒數（預設 {DEFAULT_DELAY:g}）")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE, metavar="SEC",
                    help=f"長度差容許秒數（預設 {DEFAULT_TOLERANCE}）")
    ap.add_argument("--keep-cover", action="store_true", help="保留原本的封面")
    ap.add_argument("--all", action="store_true", help="標籤已齊全的檔案也重新查")
    args = ap.parse_args()
    return cmd_apply(args) if args.apply else cmd_scan(args)


if __name__ == "__main__":
    sys.exit(main())
