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
3. **Chrome 登入蝦皮**：設定頁按「啟動 Chrome」，工具會用一個**獨立的瀏覽器設定檔**（存在 `data\chrome-profile`，跟你平常用的 Chrome 完全分開——不同的登入狀態、不同的書籤/擴充功能、不同的程序，兩邊互不干擾、可以同時開著）啟動一個新的 Chrome 視窗並開啟除錯埠讓工具讀取頁面資料。
   1. 視窗開啟後預設停在 shopee.co.id 首頁，是全新未登入狀態 → 照平常方式登入（帳密 + 簡訊/Email OTP 等蝦皮要求的驗證），只需要做這一次
   2. 登入狀態會存在 `data\chrome-profile` 資料夾裡，**下次啟動工具不用再登入**；就算不小心把這個 Chrome 視窗整個關掉，工具下次要用時也會自動用同一個設定檔重新啟動，登入狀態不會不見
   3. 執行任務時這個視窗可以縮到背景，不需要一直盯著；只有遇到**驗證碼**或**登入過期**時，工具才會自動把它拉到前景並在網頁介面跳紅色提醒，屆時再過去手動處理，處理完回介面按「續跑」即可（見下方「日常操作」）
   4. 這個設定檔建議保持乾淨，不要另外裝廣告攔截器等擴充功能——它們可能干擾工具攔截頁面請求的機制
   5. 如果哪天想強制重新登入（例如懷疑帳號需要重新驗證），把 `data\chrome-profile` 整個資料夾刪掉，再按一次「啟動 Chrome」重新走登入流程即可
4. **錄製位置篩選參數**：在該 Chrome 搜尋任意商品 → 設定頁按「開始錄製」→ 手動套用 Shipped From 篩選（More → Others → 勾三個 Tangerang + 五個 Jakarta → CONFIRM）→ 工具自動記下參數。
5. **拉取資料**：設定頁按「從 Google Sheet 拉取」（離線測試可用「從本地 xlsx 匯入」）。

## 日常操作

- **商品頁**：篩選「有效連結 &lt; 3」→ 勾選 → 「檢查勾選商品」或「為勾選商品找連結」；商品明細頁可編輯基準成本覆寫與 X% 門檻（預設存本地，不動 Google Sheet 結構）。
- **任務頁**：看進度、暫停/續跑/停止。遇到驗證碼會自動暫停並跳紅色橫幅——到 Chrome 視窗人工解掉後按「續跑」。程式中途關掉也沒關係，重啟後到任務頁按「續跑」。
- **找連結的完整流程（重要）**：按「找連結」後，工具只會把找到的候選連結放進**審核頁**（不會自動寫進 Google Sheet）。要補進表格必須到審核頁**採用**候選（產生「插入新列」的待寫入），再按**套用**才會真正插到 Sheet。所以「找連結」跑完後如果 Sheet 沒變，是正常的——去審核頁採用候選即可。若某商品第一頁找不到足夠的相似商品（例如關鍵字太冷門），會標「需人工設定關鍵字」，屬 SPEC 允許的「不滿 3 個可停止」情況。
- **審核頁**：dry-run 模式（預設開）下，所有要寫進 Google Sheet 的變更（改價、Note、插入新列）都先停在這裡，確認後按「套用」才真正寫入。候選新連結也在此採用/拒絕。
- **前端會在任務完成後自動重整**：商品頁/明細頁/審核頁在偵測到正在追蹤的任務結束後會自動重新載入一次，顯示最新價格與狀態（不必手動 F5）。
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
