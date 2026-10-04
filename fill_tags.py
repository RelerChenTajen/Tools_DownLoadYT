#!/usr/bin/env python3
"""
fill_tags.py - 用「演唱者 + 曲名」搜尋 YouTube Music，補齊 MP3 的專輯、發行年份與正方形專輯封面

只改 ID3 標籤，不重新編碼，音質與音量不變。

用法:
    python fill_tags.py                       # 預設：掃描 output/*.mp3，只列出建議（不寫入）
    python fill_tags.py --apply               # 確認後實際寫入
    python fill_tags.py "output/某首.mp3" --apply
    python fill_tags.py --tolerance 30        # 長度差容許秒數（預設 60；MV 與專輯版常差數十秒）
    python fill_tags.py --keep-cover          # 不換封面
"""
import argparse
import glob
import os
import re
import sys
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
    if not (artist and title):
        stem = os.path.splitext(os.path.basename(path))[0].replace("⧸", "/")
        if " - " in stem:
            a, t = stem.split(" - ", 1)
            artist, title = artist or a.strip(), title or t.strip()
    return {
        "artist": artist, "title": title, "album": get("TALB"), "year": get("TDRC")[:4],
        "duration": MP3(path).info.length,
        "has_cover": any(k.startswith("APIC") for k in tags.keys()),
    }


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


def write_tags(path: str, m: dict, cover: bytes | None):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TALB", [TALB(encoding=3, text=m["album"])])
    if m["year"]:
        tags.setall("TDRC", [TDRC(encoding=3, text=m["year"])])
    # 原本沒有演唱者/曲名（從檔名拆出來的）才補上，不覆蓋既有值
    if "TPE1" not in tags:
        tags.add(TPE1(encoding=3, text=m["file_artist"]))
    if "TIT2" not in tags:
        tags.add(TIT2(encoding=3, text=m["file_title"]))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(path, v2_version=3)


def main() -> int:
    ap = argparse.ArgumentParser(description="用 YouTube Music 補齊 MP3 的專輯、年份、封面")
    ap.add_argument("files", nargs="*", help=f"MP3 檔（預設 {DEFAULT_OUTDIR}/*.mp3）")
    ap.add_argument("--apply", action="store_true", help="實際寫入（預設只列出建議）")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE, metavar="SEC",
                    help=f"長度差容許秒數（預設 {DEFAULT_TOLERANCE}）")
    ap.add_argument("--keep-cover", action="store_true", help="保留原本的封面")
    args = ap.parse_args()

    files = args.files or sorted(glob.glob(os.path.join(DEFAULT_OUTDIR, "*.mp3")))
    if not files:
        print("沒有 MP3 檔")
        return 1

    yt = YTMusic()
    found = 0
    for path in files:
        name = os.path.basename(path)
        cur = read_current(path)
        if not (cur["artist"] and cur["title"]):
            print(f"✗ {name}\n    無法判斷演唱者/曲名，略過\n")
            continue
        try:
            m, note = find_match(yt, cur, args.tolerance)
        except Exception as e:  # 網路或 API 變動
            print(f"✗ {name}\n    搜尋失敗：{e}\n")
            continue
        if not m:
            print(f"✗ {name}\n    {note}（{cur['artist']} / {cur['title']}，{cur['duration']:.0f}s）\n")
            continue
        found += 1
        m["file_artist"], m["file_title"] = cur["artist"], cur["title"]
        print(f"✓ {name}  （{note}）")
        print(f"    對應: {' / '.join(m['artists'])} - {m['title']}  "
              f"[{m['duration']}s，檔案 {cur['duration']:.0f}s]  https://music.youtube.com/watch?v={m['video_id']}")
        print(f"    專輯: {cur['album'] or '（無）'} → {m['album']}  ({m['type']})")
        print(f"    年份: {cur['year'] or '（無）'} → {m['year'] or '（無）'}")
        if not args.keep_cover:
            print(f"    封面: {'影片截圖' if cur['has_cover'] else '（無）'} → 專輯封面 {COVER_SIZE}×{COVER_SIZE}")

        if args.apply:
            cover = None
            if not args.keep_cover and m["cover"]:
                with urllib.request.urlopen(m["cover"], timeout=30) as resp:
                    cover = resp.read()
            write_tags(path, m, cover)
            print("    已寫入")
        print()

    print(f"相符 {found} / {len(files)} 首" + ("" if args.apply else "；確認無誤後加 --apply 寫入"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
