# Investment Red-Team（投資紅隊開源框架）

> **在真金白銀進場之前，先讓最冷酷的空頭把你的投資論點打穿。**

`investment-red-team` 是一套開源、零個資、跨市場的**投資決策壓力測試（Adversarial Thesis Stress-Testing）框架**。它將資安領域的「紅隊演練（Red Teaming）」導入個人投資流程，協助投資者在建立倉位前系統性揭露盲點、量化論點失效條件（Kill-Switch），並防止確認偏誤（Confirmation Bias）主導資金配置。

---

## 核心特色

1. **投資紅隊開源方法論（[`framework/`](./framework/)）**
   - 六階段紅隊審計流水線：論點鋼人化（Steelman）→ 紅隊反方攻擊 → 事前驗屍（Pre-Mortem）→ 量化存活評分 → Kill-Switch 失效門檻 → 倉位紀律閘門。
   - 內建 12 大投資認知偏誤檢核表與多角色 AI 紅隊攻防 Prompt 庫。
2. **四大市場專用審計範本（[`templates/`](./templates/)）**
   - **台股（`tw-stock.md`）**：月營收動能、法說會指引落差、三大法人與融資籌碼、高殖利率價值陷阱、匯率與家族治理風險。
   - **美股成長股（`us-growth.md`）**：Rule of 40、SBC 股權稀釋、NRR/留存率、FCF 轉換率、估值倍數壓縮與護城河侵蝕。
   - **ETF（`etf.md`）**：追蹤誤差、真實總費用率（TER）、前十大成分股集中度、換股週轉率、折溢價與配息來源檢查。
   - **加密貨幣（`crypto.md`）**：代幣經濟學（FDV vs 流通市值）、解鎖拋壓、協議真實費用收入、智能合約／託管／監管與流動性風險。
3. **跨市場報價抓取 CLI（[`cli/quote_fetch.py`](./cli/quote_fetch.py)）**
   - 純 Python 標準函式庫實作（**零外部套件依賴、預設免 API Key**）。
   - 支援台股（`tw`）、美股（`us`）、ETF（`etf`）、加密貨幣（`crypto`），可輸出終端表格、JSON 或可直接貼入紅隊報告的 Markdown 報價快照。
4. **零個資隱私檢查 `privacy-check`（[`cli/privacy_check.py`](./cli/privacy_check.py)）**
   - 自動掃描本機／暫存區檔案是否誤含個人敏感資訊：真實持股金額、券商帳號、台灣身分證字號、手機／Email、個人私密目錄（如 `my-cases/`、`portfolio/`）或對帳單內容。
   - 附帶 Git `pre-commit` hook 與 GitHub Actions CI 強制閘門，確保公開倉庫永遠維持**零個資（Zero PII）**。
5. **互動式紅隊審計儀表板（[`dashboard/index.html`](./dashboard/index.html)）**
   - 單檔零後端 Web App（本機雙擊開啟或部署至 GitHub Pages 皆可使用）。
   - 內建四大市場紅隊檢核表、六維度風險雷達圖、0–100 紅隊存活分數計算、倉位上限建議、Kill-Switch 觸發條件管理，以及 Markdown／JSON 一鍵匯出（資料僅存於瀏覽器本地，絕不上傳）。

---

## 目錄結構

```text
investment-red-team/
├── framework/                  # 紅隊方法論、評分標準、認知偏誤與 AI Prompt 庫
├── templates/                  # 台股 / 美股成長股 / ETF / 加密貨幣四大市場範本
├── examples/                   # 完全去識別化的示範報告（虛構標的）
├── cli/
│   ├── quote_fetch.py          # 跨市場報價抓取 CLI
│   └── privacy_check.py        # 零個資隱私檢查 CLI
├── dashboard/
│   └── index.html              # 互動式紅隊審計儀表板（單檔零依賴）
├── scripts/
│   └── install-hooks.sh        # 安裝 Git pre-commit 隱私檢查勾子
├── tests/                      # 單元測試套件
└── .github/workflows/ci.yml    # 自動測試 + 隱私掃描 CI
```

---

## 快速開始

### 環境需求
- Python `>= 3.10`（無需安裝任何第三方套件即可執行全部 CLI 與測試）

### 1. 抓取跨市場報價快照
```bash
# 抓取台股、美股、ETF、加密貨幣報價並以表格顯示
python3 -m cli.quote_fetch --tw 2330 --us NVDA --etf 0050.TW,VOO --crypto bitcoin,ethereum

# 輸出為 Markdown 區塊，方便直接貼進紅隊審計報告
python3 -m cli.quote_fetch --tw 2330 --us AAPL --crypto bitcoin --format markdown

# 離線環境或測試流程可用 --mock 模式
python3 -m cli.quote_fetch --tw 2330 --us NVDA --etf VOO --crypto bitcoin --mock
```

### 2. 執行零個資隱私檢查（`privacy-check`）
```bash
# 掃描整個倉庫
python3 -m cli.privacy_check --all

# 僅掃描已 git add 的 staged 檔案（適合 commit 前檢查）
python3 -m cli.privacy_check --staged

# 安裝 Git pre-commit hook，確保每次 commit 前自動把關
bash scripts/install-hooks.sh
```

### 3. 開啟互動式儀表板
直接用瀏覽器開啟 [`dashboard/index.html`](./dashboard/index.html)，或在本機啟動靜態預覽：
```bash
python3 -m http.server 8080 --directory dashboard
```

### 4. 執行單元測試
```bash
python3 -m unittest discover -s tests -v
```

---

## 公私分離架構建議

強烈建議將**框架工具**與**個人真實持股**徹底分離於兩個倉庫：

| 倉庫 | 可見性 | 內容 |
| :--- | :--- | :--- |
| **`investment-red-team`**（本倉庫） | **Public** | 開源方法論、四大市場範本、報價 CLI、`privacy-check`、互動式儀表板（**嚴格零個資**） |
| **`investment-red-team-private`** | **Private** | 個人真實持股帳本、`my-cases/` 實戰個案、季度覆盤與行為紀錄 |

---

## 免責聲明

本專案所有文件、範本、評分模型與工具僅供投資研究方法論與風險管理教學之用，**不構成任何證券、ETF 或加密資產之買賣建議**。投資決策請獨立判斷並自負風險。
