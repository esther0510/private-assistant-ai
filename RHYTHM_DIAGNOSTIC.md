# 音遊診斷模式 / Rhythm Game Diagnostic

此功能只加入本機開發版本，入口在主視窗「音遊診斷」頁籤。

## 使用方式

1. 重新啟動本機開發版本。填「遊戲設定檔」（遊戲版本、判定／offset 換算規則）及完整譜面名稱、難度、速度。
2. 描述 A、B 設定；例如 A=2560×1440 原生，B=1920×1080。一次只改一項，保持音訊、offset、鍵盤、FPS 上限、背景程式等一致，勾選確認。
3. 建議交錯 A/B，不要先連打全部 A 再全部 B。暖身後每组至少 3 局，最好各 5–10 局。
4. 選本局 A 或 B，依遊戲選單確認顯示模式。按「開始本局」後 5 秒內切回遊戲；程式將鎖定當時前景視窗，不會隨切回助理而改成監測助理。若誤選其他程式，取消本局。
5. 完成遊玩後切回助理，按「結束本局並填結果」。手填 accuracy（0–100）、判定數、總判定數、fast/slow、平均 offset 或 timing SD。空白保持未知，不算零。只有 Miss 與總判定數都有值才計算 Miss rate。
6. 選用：匯入本局 CSV 樣本；可包含 `frametime_ms`、`offset_ms`、`keydown_timestamp_ms`，欄位可獨立使用，空白略過。offset 必須換成毫秒、正值為慢、負值為快。CSV 樣本統計優先於手填統計。
7. 每局自動儲存；頁面下方即為報告。可「開啟本機測試」續測或查看；「新 A/B 測試」保留舊檔案。設定檔也可另存／載入。已記錄後鎖定譜面與共通條件，變更時建立新測試。

取消結果輸入後本局仍保留於記憶體，但監測已停止；再次按結束可填寫。取消本局／退出程式會捨棄尚未存檔的該局。已完成局數不受影響。

## 自動取得與限制

| 指標 | 方式與限制 |
|---|---|
| 遊戲程式、標題、PID、活動分類 | 沿用現有前景監控辨識；由使用者開始時選定遊戲，無需分類器先認得遊戲 |
| 目前螢幕解析度、遊戲 client 像素尺寸、螢幕裝置 | Windows 唯讀 API；client 尺寸不是遊戲內部渲染解析度，內部縮放請寫在 A/B 描述 |
| Windows DPI 縮放、刷新率 | 遊戲所在螢幕的設定，並非 FPS 或輸入延遲 |
| 視窗模式 | 可識別一般視窗、全螢幕覆蓋；獨佔全螢幕／無邊框不能僅由幾何區分，保存使用者手動確認值 |
| CPU 負載 | 每秒非阻塞取樣，保存全系統與遊戲 CPU 百分比，遊戲負載按邏輯核心數正規化；包含開始到結束期間，非精確歌曲區段 |
| 鍵盤 | 只列舉裝置介面名稱；可能同一實體鍵盤有數個介面，無法指認本局實際使用哪一台 |
| FPS、frametime、spike rate | 不自動取得；匯入遊戲／外部工具本局資料後計算，或手填 P99；沒有啟動 PresentMon 等外部監控器 |
| 掉幀、GPU load | 目前未取得，不用 GUI timer 抖動冒充遊戲掉幀 |
| Keydown timestamp、間隔 jitter | 不自動蒐集全域按鍵；只接受使用者提供 CSV。jitter 是相鄰按鍵間隔的樣本 SD，會受到譜面節奏影響，不等於 timing SD |
| Accuracy、判定、offset、timing SD | 手填遊戲結果，或由本局 offset 樣本計算；不做 OCR、不讀遊戲記憶體 |
| 結果區域 | 設定檔可保存 client 像素 x,y,width,height；目前是擴充用中介資料，不自動截圖或辨識 |

未取得欄位保持 null 或不存在，不會當成 0。沒有任何新鍵盤 hook、按鍵注入、Raw Input 註冊、遊戲注入或遊戲設定變更。

CSV 例子（列可以留白；不要將不同局拼成一局）：

```csv
frametime_ms,offset_ms,keydown_timestamp_ms
6.94,-3,1000
7.10,2,1125
30.00,5,1250
```

只匯入同一局的有效遊玩時間。PresentMon 等工具的原始欄位可能不同，必須先整理成上面的單位與欄位，不能直接匯入未核對的原始檔。沒有逐按鍵共同時間軸時，相關係數只表示「每局」spike 比例與 accuracy 損失共變，不代表 spike 與某個 Miss 的時間同步。

## 分析規則

- A/B 各至少 3 局，且至少一個共同結果指標每組都有 3 筆，才允許正式分析。僅有 CPU／frametime 不足以判斷準度。譜面不一致、組內顯示設定變動、記錄中環境變更或未確認控制條件，一律顯示暫時趨勢。
- 每局統計再算每組平均、局間 SD、有效 n。不是把所有 note 混合後當作獨立測試局數。
- 設定差異需超過實用門檻（accuracy／miss rate 0.5 百分點，offset 絕對偏移／timing SD 5 ms）以及兩倍組間平均差的標準誤。這是可解釋的啟發式，不是正式因果或顯著性檢定。
- 有結果惡化且同組 frametime P99 至少增加 3 ms，標記系統／顯示因素較可疑；只有結果差異則保留為設定相關、原因待確認。spike 與 accuracy 損失相關 r≥0.7 增加證據分數。
- 沒有設定差異且兩組 timing SD 都 ≥20 ms，標記打點穩定度／個人 offset 較可疑。SD 門檻只作初步篩檢，不適用所有遊戲難度。
- 若局間噪音太大，要求增加局數，不因沒有顯著差异就認定設定完全等效。其他相近結果顯示「非解析度主因（目前未見明顯設定差異）」並保留未量測因素的限制。
- P99 使用線性插值；spike 是超過本測試固定門檻的 frame 百分比（預設 25 ms，可依遊戲需求設定）。平均 FPS=1000/平均 frametime。沒有足夠 frame 樣本時，P99 可靠性有限。
- 信心分數 0–85 是規則式證據完整度，不是機率。資料不足顯示 0 與暫時趨勢。

## 本機資料與開發驗證

測試 JSON 儲存在現有資料庫旁的 `rhythm_diagnostics`，每個測試獨立檔案。只包含該診斷的環境、CPU 樣本、使用者結果及匯入樣本，不上傳。報告在介面內顯示，原始數據可再讀取。

新模組：`rhythm_diagnostic.py`（資料與分析）、`rhythm_environment.py`（Windows 唯讀採集）、`rhythm_ui.py`（頁籤與輸入）。`main_window.py` 加入入口與狀態列。

針對測試：`.venv/Scripts/python.exe -m unittest discover -s tests -p "test_rhythm*.py" -v`

Windows API 參考：[Raw Input 裝置列舉](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)、[DPI awareness](https://learn.microsoft.com/en-us/windows/win32/api/windef/ne-windef-dpi_awareness)。
