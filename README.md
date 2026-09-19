# yt2mp3 — YouTube 高品質音訊 → MP3

> 圖解版說明：雙擊 `說明.html` 用瀏覽器開啟。

## 內定值
定義在 `yt2mp3.py` 頂部：`DEFAULT_OUTDIR`、`DEFAULT_BITRATE`、`DEFAULT_VOLUME`，改常數即可變更預設。
在 Claude Code 中：貼網址或輸入 `/yt2mp3 <URL>` 即自動下載（設定見 `CLAUDE.md` 與 `.claude/skills/yt2mp3/`）。

## 需求
- Python 3.10+
- `pip install -r requirements.txt`
- ffmpeg（`winget install Gyan.FFmpeg`，腳本會自動找到 winget 安裝位置）

## 使用
```
python yt2mp3.py <URL>                  # 內定：320 kbps、Volume 97、output/、檔名「演唱者 - 曲名.mp3」
python yt2mp3.py <URL> --no-volume      # 保留原始響度
python yt2mp3.py <URL> -v 95            # 改音量目標
python yt2mp3.py <URL> -b 192           # 指定位元率
python yt2mp3.py <URL> --keep-original  # 保留原始 Opus/M4A，不轉檔（音質最佳）
python yt2mp3.py --list urls.txt        # 批次下載
```
或直接雙擊 `yt2mp3.bat`，貼上網址即可。

## 檔名規則
1. YouTube 有提供 artist/track 中繼資料 → 直接採用
2. 否則拆解標題：`A - B`、`A – B`、`A【B】` → 演唱者 A、曲名 B（自動去除 `(Official Video)`、`[Lyrics]`、`| HD` 等後綴）
3. 都不符合 → 頻道名當演唱者、標題當曲名

ID3 標籤（title/artist）會同步寫入。

## 音量調整（內定 97，`--no-volume` 關閉）
用 ffmpeg 的 `replaygain` 濾鏡（與 MP3Gain 相同演算法）量測來源音訊，
在「解碼 → 增益 → 編碼 MP3」單一流程內套用精確增益，不會多一次有損轉檔。
實測目標 97 → 輸出 97.00（MP3Gain 讀值 = 89 − track_gain）。
注意：拉高音量不會做限幅，峰值可能超過 0 dBFS（MP3 可儲存但播放時可能削波）。

## 音質說明
YouTube 提供的音訊上限約 Opus 160 kbps / AAC 128 kbps（Premium 256 kbps）。
轉成 320 kbps MP3 可確保轉檔不再劣化，但不可能超越來源品質。

## 更新
YouTube 常改版，若下載失敗請先執行：`python -m pip install -U yt-dlp`
