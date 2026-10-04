# yt2mp3 — 把 YouTube 變成一樣大聲的 MP3

貼一個網址進去，出來一個 **320 kbps、音量固定在 97、名字整齊** 的 MP3。就這樣。

```
YouTube 影片  ──▶  yt2mp3.py  ──▶  演唱者 - 曲名.mp3
                  (儀表指著 97)      320 kbps
```

> 圖解版（含插圖）：雙擊 `說明.html` 用瀏覽器開啟。

---

## 它保證三件事

| | 保證 | 說明 |
|---|---|---|
| 🎚️ | **拿最好的聲音** | YouTube 有好幾條音軌，永遠挑最高品質那條（通常 Opus 約 150 kbps），再壓成 320 kbps MP3 |
| 📏 | **一樣大聲** | 每首歌都調到 MP3Gain 讀值 **97.0**。放進播放清單不用一直轉音量鈕 |
| 🏷️ | **名字整齊** | 檔名和 ID3 標籤一律「**演唱者 - 曲名**」，還附上封面圖 |

## 裡面發生什麼事：五個步驟

重點在第 3～5 步：量音量、調音量、壓成 MP3 是在**同一次**轉檔裡做完的，聲音只被壓縮一次。

| # | 步驟 | 做了什麼 |
|---|---|---|
| 1 | **找到影片** | yt-dlp 去 YouTube 問「這個網址有哪些音軌？」YouTube 會出一道 JavaScript 小考題，交給 Node.js 解 |
| 2 | **只拿聲音，不拿畫面** | 抓最高品質的音軌（Opus，幾 MB 而已），影片畫面完全不下載，所以很快 |
| 3 | **量一下有多大聲** | ffmpeg 用 ReplayGain 演算法（跟 MP3Gain 一模一樣的尺）聽完整首歌，算出「現在是幾 dB」 |
| 4 | **算差多少，補上去** | 目標 97、現在 94.5、差 2.5 → 轉檔時把音量加 2.5 dB。太大聲的歌則往下減 |
| 5 | **壓成 MP3，貼上名牌** | 320 kbps MP3，寫入演唱者、曲名，把影片縮圖當封面塞進去，存到 `output/` |

## 為什麼是 97

```
調整前                          調整後
  ▂  █  ▅  ▁                      █  █  █  █   ← 全部對齊 97.0
 91  96.9 94.5 85                97  97  97  97
```

MP3Gain 用 89 當「標準音量」，數字越大越大聲。你手上的舊收藏在 96～97 之間（例如 `4 Non Blondes - What's Up.mp3` 是 96.6），所以新下載的也對齊到 97，前後不會突然一大一小。

```
Volume = 89 − ReplayGain track_gain
```

MP3Gain 只能一次調 1.5 dB；這個工具是在轉檔前用小數點精準調整，實測誤差 0.02 以內。

## 檔名怎麼來的

```
YouTube 標題:  4 Non Blondes - What's Up (Official Music Video)
                                          ~~~~~~~~~~~~~~~~~~~~~~ 去掉雜訊
                              ↓ 用「 - 」切開
檔名:          [4 Non Blondes] - [What's Up].mp3
                  演唱者            曲名
```

1. YouTube 偶爾會直接給演唱者和曲名（artist / track 中繼資料），有就用
2. 沒有的話拆標題：先丟掉 `(Official Video)`、`[Lyrics]`、`| HD`、`Official MV` 這類字，再從「 - 」「 – 」或「【 】」切成兩半
3. 都切不開 → 頻道名當演唱者、整個標題當曲名（這種情況偶爾要手動改名）

ID3 標籤（title / artist）會同步寫入。

## 怎麼用：三種方式

### 最簡單：在 Claude Code 裡直接貼網址
專案的 `CLAUDE.md` 已寫好規則：看到 YouTube 網址就下載、驗證、回報。也可以打 `/yt2mp3 <網址>`。

### 不開終端機：雙擊 `yt2mp3.bat`
跳出黑視窗，貼上網址按 Enter，等它跑完。

### 命令列
```
python yt2mp3.py "https://www.youtube.com/watch?v=xxxx"
```

| 參數 | 作用 |
|---|---|
| `--no-volume` | 不調音量，保留 YouTube 原本的大小聲 |
| `--no-limit` | 拉高音量後不壓峰值（可能削波） |
| `-v 95` | 改成別的目標音量（預設 97） |
| `-b 192` | 改位元率（預設 320） |
| `--keep-original` | 不轉 MP3，直接存 Opus 原檔（音質最好，但相容性差） |
| `--list urls.txt` | 一次下載很多首，每行一個網址 |
| `-o 資料夾` | 改輸出位置（預設 `output/`） |

### 補齊專輯、年份、專輯封面

從 MV 下載的歌常沒有專輯，年份也只是影片上傳年份。`fill_tags.py` 會用「演唱者 + 曲名」搜尋 YouTube Music，找出原始專輯：

```
python fill_tags.py                      # 掃描 output/，結果寫進 fill_tags_report.csv（不動 MP3）
python fill_tags.py "D:\某資料夾" --batch 30   # 掃描其他資料夾，每次 30 首；重複執行會接續
python fill_tags.py --apply              # 依報告中 apply=Y 的列寫入
```

分兩階段：先掃描產生報告，可用 Excel 開啟檢查，把不要寫入的列 `apply` 改成 `N`；`--apply` 照報告寫入、不重新搜尋，看到的就是寫入的。
只改 ID3 標籤（演唱者、曲名、專輯、發行年份、正方形專輯封面），不重新編碼，音質與音量不變。

報告 `status`：`ok` 相符；`warn` 需確認（長度差大、或只找到精選輯／合輯，後者預設 `apply=N`）；`notfound` 找不到。

> Windows「文件」資料夾若開啟了勒索軟體防護（受控資料夾存取），Python 無法寫入；先把檔案複製到 `output/` 處理，再用檔案總管複製回去。

## 用了哪些零件（都是免費的）

| 零件 | 角色 |
|---|---|
| **yt-dlp** | 負責跟 YouTube 講話、把音軌抓下來。YouTube 常改版，它也常更新 |
| **ffmpeg** | 聲音的瑞士刀：量音量、調音量、轉成 MP3、貼標籤和封面，全靠它 |
| **Node.js** | 幫 yt-dlp 解 YouTube 出的 JavaScript 考題，不然會缺格式 |
| **Python** | 把上面三樣串起來的 200 行腳本，就是 `yt2mp3.py` |
| **ytmusicapi / mutagen** | `fill_tags.py` 用來搜尋 YouTube Music、寫入 ID3 標籤 |

## 三個小提醒

**推太大聲會自動壓峰值**
來源只有 91 的歌要推 +6 dB 才到 97，最高點會撞到 0 dB 天花板。腳本會自動加 limiter 把峰值壓在 −1 dBFS 附近，響度仍對齊 97。想保留未壓縮的動態就用 `-v 95` 或 `--no-limit`。

```
 0 dB ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  ← 天花板
        ╭╮   ▔▔▔▔▔ 削平了
      ╭╯ ╰╮╭╯     ╰╮
```

**網址帶 `list=` 會抓整份清單**
從播放清單點進來的網址後面常有 `&list=...`，那會下載幾十首。只要一首就把它刪掉。

**壞了先更新**
YouTube 改版時 yt-dlp 會失效。執行 `python -m pip install -U yt-dlp` 通常就好。

> 音質天花板永遠是 YouTube 給的那條音軌（約 150 kbps Opus）。320 kbps MP3 是為了「不再變差」，不是「變得更好」。只下載你有權使用的內容。

---

# 技術細節

## 安裝
- Python 3.10+
- `pip install -r requirements.txt`
- ffmpeg：`winget install Gyan.FFmpeg`（腳本會自動找到 winget 安裝位置，不用重開終端機）
- Node.js（yt-dlp 的 JS runtime；已安裝的話不用動）

## 內定值
定義在 `yt2mp3.py` 頂部：`DEFAULT_OUTDIR`、`DEFAULT_BITRATE`、`DEFAULT_VOLUME`、`DEFAULT_PEAK_LIMIT`，改常數即可變更預設。
在 Claude Code 中的行為由 `CLAUDE.md` 與 `.claude/skills/yt2mp3/SKILL.md` 定義。

## 音量調整的實作
用 ffmpeg 的 `replaygain` 濾鏡（ReplayGain 1.0，與 MP3Gain 相同演算法）量測來源音訊，
在「解碼 → 增益 → 編碼 MP3」單一流程內套用精確增益（`GainedExtractAudioPP`），不會多一次有損轉檔。
實測目標 97 → 輸出 97.00 ± 0.02。

增益後預估峰值超過 `DEFAULT_PEAK_LIMIT`（−1 dBFS）時，在增益後接 `alimiter`（lookahead limiter，關閉自動拉高、補償延遲）。
limiter 會吃掉一點響度，所以先用割線法反覆量測「增益 + limiter」的結果、修正增益，讓輸出仍落在 97 ± 0.05。
MP3 編碼會讓峰值略為回升（實測 −1 dBFS → 約 0.99），仍低於 0 dBFS。峰值本來就夠低的歌不會經過 limiter。

量測任一檔案的 MP3Gain 讀值：
```
ffmpeg -i <file> -af replaygain -f null -      # Volume = 89 − track_gain
```

## 命名邏輯
在 `derive_artist_track()`；遇到解析不佳的標題，改該函式而不是手動改檔名。

## 補標籤的比對規則（`fill_tags.py`）
1. 標籤讀演唱者與曲名；沒有標籤時從「演唱者 - 曲名」檔名拆（也接受「演唱者- 曲名」）。標籤已齊全的檔案預設略過（`--all` 可重查）。
2. YouTube Music 歌曲搜尋，演唱者與曲名正規化後須相符；標題含 Live、Karaoke、Remix、Re-Recorded 等版本字樣（原曲名沒有時）排除。
3. 長度差在 `--tolerance`（預設 60 秒）內的候選中，取發行年份最早者（原版優於精選輯）；全部超出時取最接近者並標 ⚠。
4. 再查演唱者的專輯清單，若有更早收錄同名曲的專輯則改用它。
5. 專輯名稱像精選輯／合輯（Greatest、Best of、Hits、Collection、Vol. 等）時標 ⚠ 且預設不寫入。
6. 寫入 TALB、TDRC 與 1200×1200 JPEG 封面，以 ID3v2.3 儲存。已有的演唱者/曲名不覆蓋；沒有時採 YouTube Music 正式寫法
   （曲名去掉原檔名沒有的括號說明，例如 `(From "Dirty Dancing" Soundtrack)`；檔名列出的合唱者較多時沿用檔名）。

## 音質說明
YouTube 提供的音訊上限約 Opus 160 kbps / AAC 128 kbps（Premium 256 kbps）。
轉成 320 kbps MP3 可確保轉檔不再劣化，但不可能超越來源品質。

## 疑難排解
- 下載失敗先更新：`python -m pip install -U yt-dlp`
- 出現「Remote component challenge solver script was skipped」警告：腳本已設定 `remote_components: ["ejs:github"]` 自動下載解算腳本；若仍出現，確認 Node.js 在 PATH 中
