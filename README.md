# Indo Shopee 連結及基準成本管理系統

依 [SPEC.md](SPEC.md) 實作：本地網頁介面管理 Google Sheet「purchase link」分頁的採購連結——驗證連結（invalid / sold out / unlisted / high cost）、回填最新 IDR 價格、依基準成本判斷高價，並在有效連結 ≤1 時自動到印尼蝦皮搜尋補連結。

## 啟動

雙擊 **`app.pyw`**（無 cmd 視窗）→ 自動開啟瀏覽器 `http://127.0.0.1:8765`。
除錯時改用終端機執行 `python app.pyw` 可看即時 log（log 檔在 `data\logs\app.log`）。

## 首次設定（一次性）

1. **安裝依賴**（用系統 Python，**不要建 venv**——Smart App Control 會封鎖 venv 內的原生 DLL）：
   ```
   pip install --user openpyxl
   pip install google-auth==2.29.0
   ```
   （其餘 Flask / gspread / Pillow / websocket-client 本機已有。google-auth 必須釘 2.29.0，新版會 import 被 Smart App Control 封鎖的 cryptography DLL。）
2. **Google 授權**：這個工具沒有經過 Google 官方驗證（單人使用的本地工具沒必要送審），所以一定要先把自己設成「測試使用者」，不然點下去會卡在「Google 尚未驗證這個應用程式」的警告頁面。到 [Google Cloud Console](https://console.cloud.google.com/) 依序：
   1. 建立新專案（右上角專案選單 → 新增專案）
   2. 「API 和服務」→「已啟用的 API 和服務」→「+ 啟用 API 和服務」→ 搜尋 **Google Sheets API** → 啟用
   3. 「API 和服務」→「OAuth 同意畫面」→ User Type 選 **外部**（External）→ 填應用程式名稱等基本資料 → 一路下一步到「測試使用者」（Test users）畫面 → 按「+ ADD USERS」把你自己的 Google 帳號（`mamiclores@gmail.com`）加進去 → 儲存。保持在 **Testing** 發布狀態即可，不要送審。
   4. 「API 和服務」→「憑證」→「+ 建立憑證」→「OAuth 用戶端 ID」→ 應用程式類型選 **電腦版應用程式** → 建立 → 下載 JSON 存成 `data\client_secret.json`
   5. 設定頁按「連結 Google 帳號」→ 瀏覽器彈出的畫面仍會顯示「Google 尚未驗證這個應用程式」（這是正常的，因為沒有送審，不是設定錯誤）→ 點左下角**「進階」**→ 點**「前往〈應用程式名稱〉（不安全）」**→ 用步驟 3 加的測試帳號繼續完成授權
   
   之後 token 會快取在 `data\google_token.json`，不用每次重新授權。
3. **Chrome 登入蝦皮**：設定頁按「啟動 Chrome」（會用獨立的 profile 開啟，不影響平常的 Chrome）→ 在該視窗登入 shopee.co.id 一次。
4. **錄製位置篩選參數**：在該 Chrome 搜尋任意商品 → 設定頁按「開始錄製」→ 手動套用 Shipped From 篩選（More → Others → 勾三個 Tangerang + 五個 Jakarta → CONFIRM）→ 工具自動記下參數。
5. **拉取資料**：設定頁按「從 Google Sheet 拉取」（離線測試可用「從本地 xlsx 匯入」）。

## 日常操作

- **商品頁**：篩選「有效連結 ≤1」→ 勾選 → 「檢查勾選商品」或「為勾選商品找連結」；商品明細頁可編輯基準成本覆寫與 X% 門檻（預設存本地，不動 Google Sheet 結構）。
- **任務頁**：看進度、暫停/續跑/停止。遇到驗證碼會自動暫停並跳紅色橫幅——到 Chrome 視窗人工解掉後按「續跑」。程式中途關掉也沒關係，重啟後到任務頁按「續跑」。
- **審核頁**：dry-run 模式（預設開）下，所有要寫進 Google Sheet 的變更（改價、Note、插入新列）都先停在這裡，確認後按「套用」才真正寫入。候選新連結也在此採用/拒絕。
- **Note 防蓋**：工具只會寫/清 `invalid`、`sold out`、`unlisted`、`high cost` 四種機器標記；I 欄的人工備註（如「6/27 Store is Holiday」）絕不覆寫，會標成 note conflict 供人工處理。

## 驗證流程建議（首次上線）

1. 保持 dry-run 開啟，先對 2-3 個商品跑「檢查」，到審核頁確認 E/I 欄變更合理。
2. 在 Google Sheet 內先複製一個「purchase link」分頁副本，把設定頁的分頁名稱改成副本名稱，套用寫入測試插列與改價，確認 F 欄公式與格式正確。
3. 改回正式分頁，跑小批次（~20 條）監督套用，沒問題再開大範圍掃描。

## 離線測試

```
python -m tracker.cli smoke        # 模組 import 冒煙測試（Smart App Control 檢查）
python -m tracker.cli import-xlsx  # 從本地 xlsx 建鏡像並列印統計
python -m tracker.cli keyword "KBT105 #14"   # 預覽搜尋關鍵字
python tests/test_pipeline.py      # 假 driver 跑完整 check/find 管線
```

## 架構速覽

單一 `pythonw` 程序：Flask (127.0.0.1:8765) + SQLite (`data\tracker.db`) + 單一背景 worker（一次一個 job，逐項可續跑）+ CDP 驅動本機 Chrome（專用 profile `data\chrome-profile`，只攔截頁面自己發的 `pdp/get_pc` 與 `search_items` XHR，不偽造請求）。所有 Sheet 寫入走 `pending_writes` 佇列：寫入前重新拉 A/H/I 欄按 (product code + shopid.itemid) 重新定位列，找不到就明確失敗，絕不按舊列號硬寫。模組說明見 `tracker/` 內各檔案 docstring。
