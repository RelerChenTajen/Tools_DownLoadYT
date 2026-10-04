#!/usr/bin/env python3
"""
yt2mp3.py - 從 YouTube 網址擷取最佳音訊並轉成 MP3

用法:
    python yt2mp3.py <URL> [<URL> ...]         # 內定：320 kbps、Volume 97、output/、「演唱者 - 曲名.mp3」
    python yt2mp3.py <URL> --no-volume         # 保留原始響度
    python yt2mp3.py <URL> --no-limit          # 增益後不加峰值限制（可能削波）
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
    # 括號內容不可跨越右括號，避免 "(I've Had) ... (Official Video)" 從第一個 "(" 一路刪到最後
    r"\s*[\(\[【（][^\)\]】）]*?(official|video|audio|lyric|hd|4k|mv|visualizer|remaster|live|"
    r"官方|字幕|歌詞|完整版)[^\)\]】）]*?[\)\]】）]\s*|\s*\|.*$",
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
DEFAULT_PEAK_LIMIT = -1.0    # dBFS；增益後峰值超過此值才啟用 limiter；用 --no-limit 關閉

MP3GAIN_REF = 89.0   # MP3Gain / ReplayGain 的參考響度 (dB)
_RG_RE = re.compile(r"track_gain\s*=\s*([-+]?[\d.]+)\s*dB")
_PEAK_RE = re.compile(r"track_peak\s*=\s*([\d.]+)")


def measure_replaygain(ffmpeg: str, path: str, pre_filter: str = "") -> tuple[float, float]:
    """回傳 (ReplayGain track gain dB, 線性峰值)。MP3Gain 的 Volume = 89 - track_gain。

    pre_filter 會接在 replaygain 前面，用來量測套用增益/limiter 之後的結果。
    """
    af = f"{pre_filter},replaygain" if pre_filter else "replaygain"
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", path, "-vn", "-af", af, "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    g, p = _RG_RE.search(out), _PEAK_RE.search(out)
    if not (g and p):
        raise RuntimeError("無法量測 ReplayGain（需要 ffmpeg >= 6.1）")
    return float(g.group(1)), float(p.group(1))


def gain_filter(delta: float, limit: float | None) -> str:
    """增益濾鏡；limit 為線性峰值上限時，後面接 lookahead limiter（不自動拉高音量、補償延遲）。"""
    f = f"volume={delta:.3f}dB"
    if limit is not None:
        f += f",alimiter=limit={limit:.4f}:attack=5:release=50:level=false:latency=true"
    return f


class GainedExtractAudioPP(FFmpegExtractAudioPP):
    """轉 MP3 時同步套用增益，使 MP3Gain 讀到的 Volume 落在目標值。

    在「解碼 → 增益 → 編碼」單一流程內完成，不會多一次有損轉檔。
    增益後峰值若超過 peak_limit (dBFS)，加上 limiter 壓住峰值，並補償 limiter 造成的響度損失。
    """

    def __init__(self, downloader=None, target_volume=None, peak_limit=None, **kw):
        super().__init__(downloader, **kw)
        self.target_volume = target_volume
        self.peak_limit = peak_limit

    def run_ffmpeg(self, path, out_path, codec, more_opts):
        if self.target_volume is not None:
            gain, peak = measure_replaygain(self.executable, path)
            current = MP3GAIN_REF - gain
            delta = self.target_volume - current
            self.to_screen(f"來源 Volume {current:.2f} dB → 目標 {self.target_volume:.1f}，套用 {delta:+.2f} dB")

            limit = None
            if self.peak_limit is not None:
                limit = 10 ** (self.peak_limit / 20)
                new_peak = peak * 10 ** (delta / 20)
                if new_peak > limit:
                    # limiter 會讓響度下降，且增益越大壓得越多：用割線法逼近目標 Volume（最多 5 次量測）
                    prev = None
                    for _ in range(5):
                        g2, _p = measure_replaygain(self.executable, path, gain_filter(delta, limit))
                        vol = MP3GAIN_REF - g2
                        miss = self.target_volume - vol
                        if abs(miss) < 0.05:
                            break
                        step = miss
                        if prev and abs(vol - prev[1]) > 1e-3:
                            step = miss * (delta - prev[0]) / (vol - prev[1])
                        prev = (delta, vol)
                        delta += max(-6.0, min(6.0, step))
                    self.to_screen(f"峰值 {new_peak:.3f} 超過 {self.peak_limit:g} dBFS，啟用 limiter，增益修正為 {delta:+.2f} dB")
                else:
                    limit = None
            more_opts = [*more_opts, "-af", gain_filter(delta, limit)]
        super().run_ffmpeg(path, out_path, codec, more_opts)


def build_opts(outdir: str, bitrate: int, keep_original: bool, ffmpeg_dir: str | None,
               target_volume: float | None = None, peak_limit: float | None = None) -> dict:
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
                                target_volume=target_volume, peak_limit=peak_limit)
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
    ap.add_argument("--no-limit", action="store_true",
                    help=f"增益後不限制峰值（預設超過 {DEFAULT_PEAK_LIMIT:g} dBFS 時啟用 limiter）")
    args = ap.parse_args()
    if args.no_volume:
        args.volume = None
    peak_limit = None if args.no_limit else DEFAULT_PEAK_LIMIT

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
        print(f"音量:   MP3Gain Volume → {args.volume} dB"
              + ("" if peak_limit is None else f"，峰值上限 {peak_limit:g} dBFS"))
    print()

    opts = build_opts(args.outdir, args.bitrate, args.keep_original, ffmpeg_dir, args.volume, peak_limit)
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
