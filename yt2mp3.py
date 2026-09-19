#!/usr/bin/env python3
"""
yt2mp3.py - 從 YouTube 網址擷取最佳音訊並轉成 MP3

用法:
    python yt2mp3.py <URL> [<URL> ...]         # 內定：320 kbps、Volume 97、output/、「演唱者 - 曲名.mp3」
    python yt2mp3.py <URL> --no-volume         # 保留原始響度
    python yt2mp3.py <URL> --bitrate 192
    python yt2mp3.py <URL> --keep-original     # 只存原始 Opus/M4A，不轉 MP3（音質最佳）
    python yt2mp3.py --list urls.txt           # 從檔案批次下載（每行一個網址）
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

try:
    import yt_dlp
    from yt_dlp.postprocessor import PostProcessor
    from yt_dlp.postprocessor import EmbedThumbnailPP, FFmpegMetadataPP
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP
except ImportError:
    sys.exit("找不到 yt-dlp，請先執行: python -m pip install -U yt-dlp")

# 標題常見的雜訊後綴，例如 "(Official Music Video)"、"[Lyrics]"、"| HD"
_NOISE = re.compile(
    r"\s*[\(\[【（].*?(official|video|audio|lyric|hd|4k|mv|visualizer|remaster|live|"
    r"官方|字幕|歌詞|完整版).*?[\)\]】）]\s*|\s*\|.*$",
    re.IGNORECASE,
)
_SEP = re.compile(r"\s+[-–—|:：]\s+")


_TAIL = re.compile(r"\s*(official\s*)?(music\s*)?(video|mv|audio|lyrics?)\s*$", re.IGNORECASE)


def clean_title(t: str) -> str:
    t = _NOISE.sub(" ", t)
    t = _TAIL.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip(" -–—")


def derive_artist_track(info: dict) -> tuple[str, str]:
    """回傳 (演唱者, 曲名)。優先用 YouTube 提供的 artist/track，其次拆解標題。"""
    artist, track = info.get("artist"), info.get("track")
    if artist and track:
        return artist, track

    title = clean_title(info.get("title") or "")
    uploader = re.sub(r"\s*-\s*Topic$", "", info.get("uploader") or info.get("channel") or "Unknown")

    # 「演唱者 - 曲名」或「演唱者【曲名】」
    parts = _SEP.split(title, maxsplit=1)
    if len(parts) == 2 and all(parts):
        return parts[0].strip(), parts[1].strip()
    m = re.match(r"^(.+?)\s*[【\[（(](.+?)[】\]）)]\s*$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # 標題以頻道名開頭：剩餘部分當曲名
    if uploader and title.lower().startswith(uploader.lower()) and len(title) > len(uploader):
        return uploader, title[len(uploader):].strip(" -–—:：")

    # 其餘：頻道名當演唱者，整個標題當曲名
    return uploader, title or info.get("id", "unknown")


class ArtistTrackPP(PostProcessor):
    """在下載前把 artist / track 填好，供檔名樣板與 ID3 標籤使用。"""

    def run(self, info):
        artist, track = derive_artist_track(info)
        info["artist"], info["track"] = artist, track
        self.to_screen(f"命名: {artist} - {track}")
        return [], info


def find_ffmpeg() -> str | None:
    """回傳 ffmpeg 所在資料夾；優先用 PATH，其次找 winget 安裝位置。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return os.path.dirname(exe)
    pattern = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin", "ffmpeg.exe",
    )
    hits = sorted(glob.glob(pattern), reverse=True)
    return os.path.dirname(hits[0]) if hits else None


# ---- 內定值（可用命令列參數覆寫）----
DEFAULT_OUTDIR = "output"
DEFAULT_BITRATE = 320        # kbps
DEFAULT_VOLUME = 97.0        # MP3Gain Volume 目標；用 --no-volume 關閉

MP3GAIN_REF = 89.0   # MP3Gain / ReplayGain 的參考響度 (dB)
_RG_RE = re.compile(r"track_gain\s*=\s*([-+]?[\d.]+)\s*dB")


def measure_replaygain(ffmpeg: str, path: str) -> float:
    """回傳 ReplayGain track gain (dB)。MP3Gain 的 Volume = 89 - track_gain。"""
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", path, "-vn", "-af", "replaygain", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    m = _RG_RE.search(out)
    if not m:
        raise RuntimeError("無法量測 ReplayGain（需要 ffmpeg >= 6.1）")
    return float(m.group(1))


class GainedExtractAudioPP(FFmpegExtractAudioPP):
    """轉 MP3 時同步套用增益，使 MP3Gain 讀到的 Volume 落在目標值。

    在「解碼 → 增益 → 編碼」單一流程內完成，不會多一次有損轉檔。
    """

    def __init__(self, downloader=None, target_volume=None, **kw):
        super().__init__(downloader, **kw)
        self.target_volume = target_volume

    def run_ffmpeg(self, path, out_path, codec, more_opts):
        if self.target_volume is not None:
            gain = measure_replaygain(self.executable, path)
            current = MP3GAIN_REF - gain
            delta = self.target_volume - current
            self.to_screen(f"來源 Volume {current:.2f} dB → 目標 {self.target_volume:.1f}，套用 {delta:+.2f} dB")
            more_opts = [*more_opts, "-af", f"volume={delta:.3f}dB"]
        super().run_ffmpeg(path, out_path, codec, more_opts)


def build_opts(outdir: str, bitrate: int, keep_original: bool, ffmpeg_dir: str | None,
               target_volume: float | None = None) -> dict:
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(outdir, "%(artist)s - %(track)s.%(ext)s"),
        "js_runtimes": {"node": {}},          # 用 Node.js 解 YouTube 的 JS 挑戰，避免格式缺漏
        "remote_components": ["ejs:github"],  # 允許自動下載/快取挑戰解算腳本（yt-dlp 官方來源）
        "windowsfilenames": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "writethumbnail": True,
    }
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir

    if not keep_original:
        # 後處理器在 main() 依序掛上：轉檔(含增益) → 標籤 → 封面
        opts["_extract"] = dict(preferredcodec="mp3", preferredquality=str(bitrate),
                                target_volume=target_volume)
    return opts


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube → MP3 下載器")
    ap.add_argument("urls", nargs="*", help="YouTube 影片或播放清單網址")
    ap.add_argument("--list", metavar="FILE", help="從文字檔讀取網址（每行一個）")
    ap.add_argument("-o", "--outdir", default=DEFAULT_OUTDIR, help=f"輸出資料夾（預設 {DEFAULT_OUTDIR}）")
    ap.add_argument("-b", "--bitrate", type=int, default=DEFAULT_BITRATE,
                    help=f"MP3 位元率 kbps（預設 {DEFAULT_BITRATE}）")
    ap.add_argument("--keep-original", action="store_true",
                    help="不轉 MP3，直接保留 YouTube 原始音訊（Opus/M4A）")
    ap.add_argument("-v", "--volume", type=float, default=DEFAULT_VOLUME, metavar="DB",
                    help=f"轉 MP3 時把 MP3Gain Volume 調到此值（預設 {DEFAULT_VOLUME:g}）")
    ap.add_argument("--no-volume", action="store_true", help="不調整音量，保留 YouTube 原始響度")
    args = ap.parse_args()
    if args.no_volume:
        args.volume = None

    urls = list(args.urls)
    if args.list:
        with open(args.list, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not urls:
        ap.print_help()
        return 1

    ffmpeg_dir = find_ffmpeg()
    if not ffmpeg_dir:
        print("錯誤: 找不到 ffmpeg。請執行 winget install Gyan.FFmpeg 後重開終端機。", file=sys.stderr)
        return 1

    os.makedirs(args.outdir, exist_ok=True)
    print(f"ffmpeg: {ffmpeg_dir}")
    print(f"輸出:   {os.path.abspath(args.outdir)}")
    print(f"格式:   {'原始音訊' if args.keep_original else f'MP3 {args.bitrate} kbps'}")
    if args.volume is not None and not args.keep_original:
        print(f"音量:   MP3Gain Volume → {args.volume} dB")
    print()

    opts = build_opts(args.outdir, args.bitrate, args.keep_original, ffmpeg_dir, args.volume)
    extract = opts.pop("_extract", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(ArtistTrackPP(), when="pre_process")
        # 依序：轉 MP3（含增益）→ 寫入標籤 → 內嵌封面
        if extract:
            ydl.add_post_processor(GainedExtractAudioPP(ydl, **extract), when="post_process")
        ydl.add_post_processor(FFmpegMetadataPP(ydl, add_metadata=True), when="post_process")
        ydl.add_post_processor(EmbedThumbnailPP(ydl, already_have_thumbnail=False), when="post_process")
        rc = ydl.download(urls)
    return rc


if __name__ == "__main__":
    sys.exit(main())
