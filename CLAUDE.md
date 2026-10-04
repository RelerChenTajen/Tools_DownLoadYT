# DownLoadYT — YouTube → MP3 專案

## 用途
把 YouTube 網址下載成高品質 MP3。工具：`yt2mp3.py`（yt-dlp + ffmpeg）。

## 固定下載設定（內定值，已寫在 yt2mp3.py 頂部的 DEFAULT_* 常數）
- 320 kbps MP3，48 kHz 立體聲
- MP3Gain Volume 調整到 **97.0 dB**（ReplayGain 演算法；在單次轉檔內套用增益，不多一次有損壓縮）
- 增益後峰值超過 −1 dBFS 自動加 limiter（並補償響度，仍為 97.0）
- 輸出到 `output/`
- 檔名與 ID3 標籤：`演唱者 - 曲名.mp3`（自動由 YouTube 中繼資料或標題拆解）
- 內嵌封面
- 使用 Node.js 作為 yt-dlp 的 JS runtime

## 使用者貼 YouTube 網址時
直接執行（不用問）：
```
PYTHONIOENCODING=utf-8 python yt2mp3.py "<URL>"
```
完成後用 ffprobe / ffmpeg replaygain 驗證，回報：檔名、位元率、長度、ID3 標籤、來源 Volume、輸出 Volume（應為 97.0 ± 0.1）、峰值。
ffmpeg 不在 PATH 時位於 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin\`，腳本會自動找到。

## 常用變化
- 保留原始響度：`--no-volume`
- 不壓峰值：`--no-limit`
- 改音量目標：`-v 95`
- 保留原始 Opus/M4A 不轉檔：`--keep-original`
- 批次：`--list urls.txt`（每行一個網址）
- 補演唱者／曲名／專輯／發行年份／正方形專輯封面：`python fill_tags.py [資料夾] --batch 30`（掃描寫進 fill_tags_report.csv）→ 檢查報告 → `--apply` 依報告寫入；只改標籤不重編碼

## 疑難排解
- 下載失敗先更新：`python -m pip install -U yt-dlp`（YouTube 常改版）
- 命名邏輯在 `derive_artist_track()`；遇到解析不佳的標題，改該函式而不是手動改檔名

## 音量換算備忘
MP3Gain Volume = 89 − ReplayGain track_gain。量測指令：
`ffmpeg -i <file> -af replaygain -f null -`
