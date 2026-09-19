---
name: yt2mp3
description: 把 YouTube 網址下載成 MP3（320 kbps、MP3Gain Volume 97、檔名「演唱者 - 曲名」）。使用者貼 YouTube 網址、說「下載成 mp3」或輸入 /yt2mp3 <URL> 時使用。
---

# yt2mp3

專案根目錄的 `yt2mp3.py` 已內建所有固定設定，直接執行即可：

```
PYTHONIOENCODING=utf-8 python yt2mp3.py "<URL>" [<URL> ...]
```

`$ARGUMENTS` 內的網址全部傳入；可含播放清單網址。若使用者另外指定：
- 「不要調音量 / 保留原始」→ 加 `--no-volume`
- 「音量調到 N」→ 加 `-v N`
- 「不要轉檔 / 保留原始格式」→ 加 `--keep-original`

## 完成後驗證與回報
對每個輸出的 mp3 執行：
```
ffprobe -v error -show_entries stream=codec_name,bit_rate -show_entries format=duration -show_entries format_tags=title,artist -of default=nw=1 "<file>"
ffmpeg -hide_banner -nostats -i "<file>" -af replaygain -f null -   # 取 track_gain / track_peak
```
回報表格：檔名、位元率、長度、ID3 標籤、來源 Volume（腳本輸出的「來源 Volume」行）、輸出 Volume = 89 − track_gain（應為 97.0 ± 0.1）、峰值 track_peak。

ffmpeg 若不在 PATH，在 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin\`。
