# Indo Shopee 連結及基準成本管理系統

## 期望輸出結果

一鍵開啟一個前端操作介面，於 Windows 背景執行(不會顯示 cmd)，可瀏覽和編輯 Google Sheet 中各個商品的基準成本和採購連結，並能判斷有效及無效之連結後，依照基準成本回填所需之連結。

## Google Sheet 連結

印尼商品連結 Indo Shopee Purchase Link 的副本
https://docs.google.com/spreadsheets/d/1DOTTepXR8OZ2wskuiz98ZE6yeplmGp0Ezk0SX_Z_tOI/edit?gid=0#gid=0

### 欄位內容

- C 欄： 蝦皮連結內的商品標題
- D 欄： 蝦皮連結內的選項(規格)，可能會有複數個，以逗號隔開
    - 若蝦皮連結內無選項，則顯示「-」
- E 欄： 商品價格(IDR)
    - **注意：必須先選好商品選項**
    - 印尼盾若顯示「114.000」實際數字是「114000」，「.」的意義並不是小數點
- F 欄： 商品價格(TW)，已有腳本會自動轉換成 TW
- G 欄： 廠商的名稱
- H 欄： 蝦皮連結
- I 欄： 商品備註


## 定義有效/無效連結

  - 前往 H 欄所記錄的連結，要能正確打開商品頁。
  - 找到正確的 "規格選項"，回填該選項的最新金額。
  - 基準金額: 表格裡同「product code」現有連結的最低金額，要可供我們自行編輯，回填金額時，如果比基準金額高 "X%" (X設計給可自行填寫) ，就要定義為無效連結(high cost)。
  - 無效連結在 Note 欄位標示無效類型 : 
      - invalid : 連結無法開啟或開啟後顯示商品已下架
          - 如連結： https://shopee.co.id/MS-GLOW-NECK-KRIM-i.175351318.6090428438?extraParams=%7B%22display_model_id%22%3A184234718887%2C%22model_selection_logic%22%3A3%7D&sp_atk=75e5bf53-9fde-43bb-a353-37b1ad99ccc3&xptdk=75e5bf53-9fde-43bb-a353-37b1ad99ccc3
      - sold out/unlisted : 連結可開啟，但商品已售完或顯示unlisted
          - Sold Out: 如連結： https://shopee.co.id/Skintific-5-AHA-BHA-PHA-Exfoliating-Toner-80ML-i.35109439.18618043618?extraParams=%7B%22display_model_id%22%3A173496272547%2C%22model_selection_logic%22%3A3%7D&sp_atk=81218b65-a954-4bc8-be49-b65663737f91&xptdk=81218b65-a954-4bc8-be49-b65663737f91
          - Unlisted： 如連結： https://shopee.co.id/MS-GLOW-DARK-SPOT-15-GR-ORIGINAL-100-BPOM-i.106057101.7529149240?extraParams=%7B%22display_model_id%22%3A22045249447%2C%22model_selection_logic%22%3A2%7D&sp_atk=3eb31277-7df9-4ff1-8883-9b1ad3cd4ac7&xptdk=3eb31277-7df9-4ff1-8883-9b1ad3cd4ac7
      - high cost : 超過基準金額 "X%" 
  - 有效連結則更新金額即可，Note 維持空白 
  - B.sold out/unlisted、C.high cost 每次都要進入有效環節，如果恢復庫存或恢復價格，可重新視為有效連結。

## 需要找連結的條件
- 該商品(product code)的有效連結只有 1 或 0 個   
- 找到最佳連結 3 個即可停止
- 找完搜尋結果第一頁仍不滿 3 個可以停止
 
## 找連結的方法

- 從原有的有效連結，抓取關鍵字，和原連結的商品圖  
- 到印尼蝦皮搜尋關鍵字
- 篩選所有包含 tagerang 和 Jakarta ，要到搜尋關鍵字左邊的「Shipped From」點擊 「More」如 image1.png，再點擊 「Other」如 image2.png，再勾選 tagerang 和 Jakarta 的商品如 image3.png 和 image4.png
- **搜尋結果要和原連結或我們的商品圖比對相似度，相似度不足則代表關鍵字錯誤，要重新擷取關鍵字搜尋**
- 找尋畫面中最便宜且銷量最高者 (1RB = 1000)
- 並且要確定連結與現有的有效連結不重複
- 價格優先於銷量作為判斷依據，但如果銷量過低 (低於100) 則不要採計	
- 如果篩選tagerang 和 Jakarta沒有好的連結，才解除掉篩選開始找其他地點 (由地理位置接近雅加達由近到遠)



## 回填及紀錄方式
- 在現有連結的最後一個，向下插入一列
- 並依序填入 Supplier Page name(C 欄)、Supplier Item name(D 欄)、Price (IDR)(E欄)、supplier's name(G欄)、Product link(H 欄)
- price (TW)已經設定好公式，向下複製即可

## 建立資料庫和介面
各項商品的有效連結除了在原表格上表示之外，也要建立一個可操作介面，讓我們可以查看和編輯各商品的訂購連結。




## 實際人工操作方式

- 發現 KBT105 #14 有兩筆於 25-26 列，不滿足三筆有效連結，缺一筆
- 查詢基準金額(最低或者是最下面一列)，所以選擇第 26 列連結之 https://shopee.co.id/-BPOM-Skintific-5X-Ceramide-Low-pH-CLEANSER-Gentle-CLEANSER-For-Sensitive-Skin-60ml-80ml-120ml-15ml-Niacinamide-Brightening-Cleanser-120ml-80ml-i.18413874.14962019307?extraParams=%7B%22display_model_id%22%3A302250486843%2C%22model_selection_logic%22%3A3%7D&sp_atk=e3fe3216-b43d-48fa-996b-9e5477e86425&xptdk=e3fe3216-b43d-48fa-996b-9e5477e86425
- 因為第 26 列的 D 欄是 {LOW PH 80ml, Skintific}，所以將滑鼠移動到 LOW PH 80ml，商品圖就會顯示成 image.png，並將畫面中的商品頁擷取下來，並且到 Indo Shopee https://shopee.co.id/ 搜尋關鍵字，關鍵字的搜尋方式要像 26 列的 C 欄叫 「[BPOM] Skintific 5X Ceramide Low pH CLEANSER Gentle CLEANSER For Sensitive Skin 60ml | 80ml | 120ml | 15ml | Niacinamide Brightening」 配合 D 欄我就會搜尋「 Skintific 5X Ceramide Low pH CLEANSER 80ml」 要有「商品 規格 容量」的關鍵字，搜尋之後透過剛剛截圖的結合找連結的方法比對相似度(至少 50%)去找到便宜且銷量最高(10RB+ sold 是最高)的商品，並回填商品連結，以這個例子最後回傳的是 27 列的結果。