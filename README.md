# 新聞時程歸檔機器人 — 從零到上線 SOP

在 **LINE** 把財經新聞整段轉貼給機器人 → 用 **Claude** 自動分類、抽出重點 → 寫進你的 **Google Sheet**。
完全不用開電腦,手機隨手歸檔。

```
你(LINE)──貼新聞──▶ 機器人(部署在雲端,24h 運作)
                        │ 1. 看第一行關鍵字分類(個股 / 產業 / 全球局勢 / 知識)
                        │ 2. Claude 抽出摘要、個股、族群、時程…
                        │ 3. 寫進對應的 Google Sheet 分頁
                        ▼
                   回覆你整理好的重點
```

---

## 📌 這份文件怎麼用

這份 SOP 同時給「人」和「Claude Code」看。最省力的方式:

1. 把這個 repo 整包下載 / clone 到本機。
2. 打開 **Claude Code**,在這個資料夾裡說:
   > 「請依照 README 的 SOP,一步一步帶我建立這個新聞機器人。」
3. Claude Code 會幫你跑指令、改設定;**需要你親自操作的部分**(開帳號、拿金鑰、刷卡)它會停下來請你做,你照著截圖回報即可。

> 程式碼已經寫好(`news_processor.py`、`bot.py`、`main.py`),你主要是「設定 + 部署」,不太需要自己寫程式。

---

## 你需要準備

| 項目 | 說明 | 費用 |
|---|---|---|
| Google 帳號 | 建試算表 + 服務帳戶 | 免費 |
| LINE 帳號 | 建官方帳號 + Messaging API | 免費 |
| Zeabur 帳號 | 跑 AI(Claude)+ 租伺服器部署 | 伺服器約 **US$10/月**;AI 預付,每篇新聞約幾毛台幣 |
| Claude Code | 跟著這份 SOP 操作 | — |

---

## 架構與檔案

| 檔案 | 作用 |
|---|---|
| `news_processor.py` | 核心:分類路由 + Claude 結構化抽取 + 寫 Google Sheet |
| `bot.py` | LINE Webhook 伺服器(Flask),收訊息、回覆 |
| `main.py` | 進入點(Zeabur 會執行 `python main.py`) |
| `requirements.txt` | Python 套件 |
| `Dockerfile` | 容器設定(Zeabur 多半用自家建置器,這個是備用) |
| `.env.example` | 環境變數範本(複製成 `.env` 填本機測試用) |

---

## 步驟一:Google Sheets + 服務帳戶

讓程式有權限寫你的試算表。

1. 開一張 Google 試算表(例如 [sheets.new](https://sheets.new)),取名(如「股票投資大腦資料庫」)。記下它的 **ID**:網址 `…/spreadsheets/d/【這串就是 ID】/edit`。
2. 到 [Google Cloud Console](https://console.cloud.google.com/) → 建立新專案。
3. 「API 與服務 → 程式庫」→ 啟用 **Google Sheets API** 與 **Google Drive API**。
4. 「API 與服務 → 憑證 → 建立憑證 → 服務帳戶」→ 取名建立(角色可略過)。
5. 點進該服務帳戶 → 「金鑰」→「新增金鑰 → JSON」→ 下載(這個檔是最高機密,別外流)。
6. 複製服務帳戶的信箱(`xxx@xxx.iam.gserviceaccount.com`)。
7. 打開試算表 → 「共用」→ 把這個信箱加為 **編輯者**。

**拿到:** 試算表 ID、服務帳戶 JSON 檔。

---

## 步驟二:LINE 官方帳號 + Messaging API

> ⚠️ LINE 已改流程:不能在開發者後台直接建頻道,要先建「官方帳號」再開啟 Messaging API。

1. 到 [LINE Developers](https://developers.line.biz/) 登入 → 建一個 **Provider**。
2. 點 **Create a Messaging API channel** → 它會引導你先去 **建立 LINE 官方帳號**(LINE Official Account)。填名稱、Email、業種(隨意)→ 建立。
3. 進入 **LINE 官方帳號管理後台** → 設定 → **Messaging API** → 啟用 → 連結到剛剛那個 Provider。
4. 回到 LINE Developers,進入這個頻道:
   - **Basic settings** → 複製 **Channel secret**
   - **Messaging API** 分頁 → **Issue** 一組 **Channel access token** → 複製
5. 用手機 LINE 掃 QR code,把這個機器人**加為好友**。
6. 在官方帳號管理後台 → **回應設定**:
   - 回應模式 = **Bot / 聊天機器人**
   - **Webhook = 啟用**
   - **自動回應訊息 = 停用**(否則會跟機器人搶著回罐頭訊息)

**拿到:** Channel secret、Channel access token。(Webhook 網址等部署後再填。)

---

## 步驟三:Zeabur AI Hub(Claude 金鑰)

Claude 透過 Zeabur AI Hub 呼叫(OpenAI 相容介面)。

1. 到 Zeabur 後台找到 **AI Hub**。
2. **先儲值**(例如 US$10)—— 注意:**要先有餘額才能建立金鑰**。
3. 建立 API 金鑰 → 複製那串 `sk-...`(只顯示一次!馬上存)。

CLI 也可以做:
```bash
npx zeabur@latest ai-hub add-balance --amount 10 -i=false
npx zeabur@latest ai-hub keys create --alias "news-bot" -i=false
```

**拿到:** AI Hub 金鑰(`sk-...`)。端點:`https://hnd1.aihub.zeabur.ai/v1`(東京),模型用 `claude-sonnet-4-5`。

> 💡 **為什麼變數叫 `OPENAI_*`?** AI Hub 提供「OpenAI 相容介面」,所以程式用 OpenAI 的 SDK、把網址指到 AI Hub。**實際呼叫到的是 Claude**,名字只是介面慣例,跟 OpenAI/GPT 無關。

---

## 步驟四:租伺服器 + 部署到 Zeabur

> Zeabur 現在需要先有一台伺服器才能建專案。

1. Zeabur 後台 → 建立新專案 → 選伺服器。
   - 最便宜約 **US$3/月**(Tencent / Volcano,**中國業者**)。
   - 在意資安可選 **Linode 東京 ~US$10/月**(美國 Akamai,離台近)。
   - 這台之後也能跑你其他小專案。
2. 在這台伺服器上建立專案(取名,如 `news-tracker`)。
3. 把本 repo 的程式部署上去(在專案資料夾用 Claude Code 或 CLI):
   ```bash
   npx zeabur@latest deploy --project-id <你的PROJECT_ID> --json
   ```
   記下回傳的 **service_id**(之後更新會用到)。
4. 在 Zeabur 後台幫這個服務 **設定環境變數**(見下方總表)。
   - `GOOGLE_CREDENTIALS_JSON` 請用 **base64**(見踩雷筆記),別直接貼 JSON。
5. 在服務的 **Networking / Domains** 產生一個公開網址(如 `https://你的名稱.zeabur.app`)。

---

## 步驟五:接上 LINE Webhook + 設白名單

1. 設定 Webhook 網址 = `https://你的網域/callback`
   - 在 LINE Developers → Messaging API → Webhook settings 填,或用 API:
   ```bash
   curl -X PUT https://api.line.me/v2/bot/channel/webhook/endpoint \
     -H "Authorization: Bearer <你的CHANNEL_ACCESS_TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"endpoint":"https://你的網域/callback"}'
   ```
2. 確認 **Use webhook 已開啟**(這個只能在後台點,沒有 API)。
3. 在 LINE 對機器人傳一個字 **`id`** → 它會回你的 LINE user id。
4. 把這個 user id 填進環境變數 `LINE_ALLOWED_USER_ID`(只允許你本人使用),然後重啟服務:
   ```bash
   npx zeabur@latest service restart --id <service_id> -y -i=false
   ```

---

## 步驟六:測試

打開 LINE 機器人,**第一行打分類關鍵字,第二行起貼新聞**:

```
個股新聞
光聖(6442)受惠CPO需求爆發,光聖三可轉債7/15掛牌…
```

機器人會回覆整理好的重點,並寫進 Google Sheet 對應分頁 🎉

---

## 環境變數一覽表(填在 Zeabur 後台)

| 變數 | 說明 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE channel access token |
| `LINE_CHANNEL_SECRET` | LINE channel secret |
| `LINE_ALLOWED_USER_ID` | 你的 LINE user id(傳「id」可查;只允許本人) |
| `OPENAI_API_KEY` | Zeabur AI Hub 金鑰 |
| `OPENAI_BASE_URL` | `https://hnd1.aihub.zeabur.ai/v1` |
| `AI_MODEL` | `claude-sonnet-4-5` |
| `GOOGLE_SHEET_ID` | 試算表 ID |
| `GOOGLE_SHEET_TAB` | (選填)個股新聞的分頁名,預設「新聞時程動態庫」 |
| `GOOGLE_CREDENTIALS_JSON` | 服務帳戶 JSON 的 **base64**(見踩雷筆記) |
| `FIRECRAWL_API_KEY` | (選填)抓連結全文用;不填則用免費的 trafilatura |
| `MAX_CONTENT_CHARS` | (選填)送進 AI 分析的內文字數上限,預設 8000 |
| `QUERY_ROWS_PER_TAB` | (選填)查詢時每個分頁取最近幾列,預設 80 |

---

## 使用方式:分類關鍵字

訊息**第一行**打關鍵字,第二行起貼內容:

| 關鍵字 | 分到分頁 | 整理出 |
|---|---|---|
| `個股新聞` | 新聞時程動態庫 | 摘要、市場(台/美/日股)、個股、概念族群、關鍵時程 |
| `產業新聞` | 產業動態 | 摘要、相關產業/族群、重點趨勢、提及個股 |
| `產業報告`(或 `券商報告`/`個股報告`/`研究報告`) | 個股產業報告 | 個股、報告日期、出具券商、目標價、近期營收、時間軸、報告總結(含**利多/利空訊號**) |
| `全球局勢` | 全球局勢動態 | 摘要、影響主題(升息/油價/關稅…)、受影響市場、時程 |
| `知識` 或 `筆記` | 知識補充庫 | 主題、重點整理、關鍵字 |

> 💡 **`產業報告`** 專為券商研究報告設計:AI 會對「漲價、新增客戶、打入供應鏈、擴產、資本支出上修、轉型」等帶動營收的字眼**高敏感度**抓成「利多訊號」,反向字眼(降價、砍單、需求疲弱…)抓成「利空訊號」,寫進報告總結方便日後篩選。

沒打關鍵字 → 機器人會提醒你標分類。分頁不存在會自動建立。

### 📎 只丟連結就自動抓全文

懶得複製整段內文時,**第一行打分類、第二行貼新聞連結**即可,機器人會自動抓該頁文章全文再分析:

```
個股新聞
https://news.cnyes.com/news/id/xxxxxxx
```

- 預設用 **trafilatura** 抽正文(免費、零設定)。
- 若設了 `FIRECRAWL_API_KEY`,會優先用 **Firecrawl**(更會處理動態/難搞網站)。
- 抓不到(需登入/擋爬蟲)→ 會提示你改貼內文,不會當掉。

### 🔍 反向查詢資料庫

想回頭問資料庫,訊息用 **「查」或「問」開頭**:

```
查 光聖最近有什麼時程
問 這週的全球局勢重點
```

機器人會讀各分頁的近期資料,交給 Claude 依資料回答(找不到會老實說沒有,不會亂編)。查詢也會參考下方「概念股主表」。

### 🏷️ 概念股主表(自動累積,免手動維護)

每次記錄**個股新聞 / 產業新聞 / 產業報告**時,機器人會自動把「個股 ↔ 概念族群」寫進一張 **「概念股主表」** 分頁,並在回覆標出該檔的已知概念與本次新增標籤:

```
🏷️ 概念股主表已更新:
• 6442 光聖 → CPO, 矽光子, 光通訊, 800G (本次新增:800G)
```

主表欄位:`個股 | 概念/族群標籤(累積去重) | 出現次數 | 首次出現 | 最近出現`。
- 個股以**代號**為去重鍵(如 6442),所以「光聖」「6442 光聖」會視為同一檔(前提是新聞有帶代號)。
- 之後就能查:`查 光聖有哪些概念` 或 `查 CPO 概念有哪些股`。
- 更新主表失敗不會影響新聞存檔(只是當次少了標籤摘要)。

---

## ⚠️ 踩雷筆記(別人會少踩很多坑)

1. **Zeabur 會忽略 Dockerfile**:它偵測到 Python 專案就用自家建置器(zbpack),預設執行 `python main.py`。所以**進入點檔名必須是 `main.py`**,改 Dockerfile CMD 沒用。
2. **Google 憑證要用 base64**:`GOOGLE_CREDENTIALS_JSON` 直接放整包 JSON,會被環境變數管線弄壞引號/換行。改放 **base64**(程式會自動偵測解碼):
   ```bash
   base64 -i credentials.json   # 把輸出整段貼進環境變數
   ```
3. **AI Hub 不支援 `response_format: json_object`**(會回空 `{}`)。本專案改用「提示詞要求 JSON + 容錯解析」,已內建,不用管。
4. **容器時區是 UTC**:處理時間會慢 8 小時。本專案已固定用 **UTC+8(台灣時間)**。
5. **AI 不知道今年幾年**:新聞若只寫月/日,AI 會猜錯年份。本專案已在提示詞帶入「今天日期」,讓它正確推斷。
6. **看錯誤用「網頁後台 → 運作紀錄」**:Zeabur CLI 的 runtime log 常只給系統事件,看不到 Python 錯誤;網頁後台的運作紀錄才看得到 traceback。
7. **服務閒置會被停**:沒流量時 Zeabur 可能把服務停掉,有訊息進來會自動起來(第一則可能稍慢)。
8. **本機測試**:若你的 Mac 是舊版系統 Python(3.9),`pip install` 可能卡在編譯 `cryptography`。建議用較新的 Python(3.11+)或直接部署到 Zeabur 測。
9. **`zeabur deploy` 只部署 git 已提交的版本**:改完程式如果沒先 `git commit` 就 deploy,線上會跑到舊碼(新功能不會生效)。**改完一定要先 commit 再 deploy。** 部署後可進容器驗證線上是新版:
   ```bash
   npx zeabur@latest service exec --id <SERVICE_ID> --env-id <ENV_ID> -i=false -- sh -c "grep -c 你改的關鍵字 news_processor.py"
   ```
10. **Windows PowerShell 跑 `npx` 被擋**:出現 `因為這個系統上已停用指令碼執行` 時,改用 `npx.cmd zeabur@latest ...`(`.cmd` 不受執行原則限制),或先 `Set-ExecutionPolicy -Scope Process Bypass -Force`。

---

## 維運(改東西之後怎麼更新)

```bash
# 改了程式 → 先提交,再重新部署(deploy 只會部署「已提交」的版本!)
git add -A && git commit -m "說明這次改了什麼"
npx zeabur@latest deploy --project-id <PROJECT_ID> --service-id <SERVICE_ID> -i=false --json
# 想讓 GitHub 也同步
git push origin main

# 只改環境變數 → 更新 + 重啟
npx zeabur@latest variable update --id <SERVICE_ID> -k "KEY=VALUE" -y -i=false
npx zeabur@latest service restart --id <SERVICE_ID> -y -i=false
```

> Windows 若 `npx` 被 PowerShell 執行原則擋住,把上面的 `npx` 改成 `npx.cmd`。

---

## 想做更多?

- ✅ ~~「只丟連結就自動抓全文」分析~~ — 已內建(trafilatura,可選 Firecrawl)
- ✅ ~~反向查詢資料庫~~ — 已內建(訊息用「查」開頭)
- 加「概念股主表」自動比對標籤
- 每日/每週盤後摘要主動推播
- 自動判斷分類(免打第一行關鍵字)
- 把其他專案也部署到同一台伺服器
- 改用 **Anthropic 官方 API 直連 Claude**(目前是透過 Zeabur AI Hub 呼叫 Claude;若想直連官方,只要改 `OPENAI_BASE_URL` 與金鑰即可——**用的同樣是 Claude,只是換呼叫管道與結帳對象**)

---

## 授權與使用規範

本 SOP 由 **Ken Hung** 於實作過程中整理而成。歡迎 fork、改造、分享,但請遵守以下原則:

- **註明出處**:使用或轉載時,請標明原作者(Ken Hung)與本專案連結。
- **非商業使用**:請勿用於營利用途。
- **請勿據為己有**:不得移除出處、把本內容當作自己的原創再行散布。
- **尊重智慧財產權**:本文涉及的第三方服務(Google、LINE、Zeabur、Anthropic 等)商標與權利,皆屬各自所有者。

> 概念上等同 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)(姓名標示—非商業性)。感謝你的善意使用 🙏
