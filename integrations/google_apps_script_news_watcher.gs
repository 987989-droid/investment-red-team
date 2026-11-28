/**
 * Investment Red-Team v2 — Google Apps Script 可信新聞突發事件監控小程式（完全免費）
 *
 * 功能：
 * 1. 定時（例如每 1 小時）向 Google News RSS 抓取關注標的之最新新聞。
 * 2. 嚴格過濾只保留 Tier-1 / Tier-2 可信媒體（Reuters、Bloomberg、WSJ、CNBC、中央社、經濟日報、工商時報、鉅亨網等）。
 * 3. 偵測高衝擊關鍵字（CEO 辭職／復職、出口禁令、關稅、砍單、財測下修、法說會等）。
 * 4. 自動將事件快報寫入 Google 雲端硬碟「Investment-Red-Team-Hub / 00_Inbox_Context」，讓 3 個 Gemini Pro 帳號與 NotebookLM 共享讀取。
 * 5. 命中高衝擊事件時，立即寄出 Gmail 警示信（已去重，同一新聞不會重複洗版）。
 *
 * 使用方式（約 2 分鐘，完全免費）：
 * 1. 前往 https://script.google.com/ -> 新增專案 -> 貼上本程式碼。
 * 2. 修改下方 CONFIG 裡的 WATCHLIST 與你的收件信箱（或留空自動寄給執行帳號自己）。
 * 3. 先手動執行一次 `runTrustedNewsWatcher()` 完成 Google Drive & Gmail 授權。
 * 4. 左側點「觸發條件（鬧鐘圖示）」-> 新增觸發條件 -> 選 `runTrustedNewsWatcher` -> 時間驅動 -> 每小時執行一次。
 */

const CONFIG = {
  // Google Drive 共用中樞資料夾名稱（需與 Drive 中建立的資料夾同名）
  ROOT_FOLDER_NAME: "Investment-Red-Team-Hub",
  INBOX_SUBFOLDER_NAME: "00_Inbox_Context",

  // 收件信箱（留空 "" 會自動寄給目前登入的 Google 帳號自己）
  ALERT_EMAIL: "",

  // 監控清單（可自由增減美股龍頭與台股供應鏈）
  WATCHLIST: [
    { query: "NVDA stock", lang: "en", label: "NVDA（AI 龍頭）", spilloverTW: "2330, 2382, 3231, 2317" },
    { query: "TSMC OR 2330 台股", lang: "zh-TW", label: "台積電 (2330)", spilloverTW: "2330, 3711" },
    { query: "Apple AAPL stock", lang: "en", label: "AAPL", spilloverTW: "2330, 2317, 3008" },
    { query: "台股 半導體 關稅 OR 禁令", lang: "zh-TW", label: "台股系統風險", spilloverTW: "0050, 2330" }
  ],

  // 可信媒體白名單（非白名單之論壇／自媒體將自動忽略）
  TIER1_SOURCES: [
    "reuters", "bloomberg", "wall street journal", "wsj", "financial times",
    "associated press", "sec", "pr newswire", "business wire", "中央社", "cna"
  ],
  TIER2_SOURCES: [
    "cnbc", "yahoo finance", "marketwatch", "barron", "nikkei",
    "經濟日報", "工商時報", "鉅亨網", "moneydj", "科技新報"
  ],

  // 重大衝擊事件關鍵字（命中即升級為 Event Shock 警示）
  SHOCK_KEYWORDS: [
    "resign", "resignation", "steps down", "ousted", "reinstated", "returns as ceo",
    "ceo", "ban", "export control", "tariff", "sanction", "antitrust", "guidance cut",
    "order cut", "investigation", "辭職", "請辭", "復職", "回歸", "執行長", "董事長",
    "換帥", "禁令", "出口管制", "關稅", "砍單", "下修", "調查", "重訊"
  ]
};

function runTrustedNewsWatcher() {
  const props = PropertiesService.getScriptProperties();
  const seenMap = JSON.parse(props.getProperty("SEEN_NEWS_KEYS") || "{}");
  const nowStr = Utilities.formatDate(new Date(), "Asia/Taipei", "yyyy-MM-dd HH:mm");
  const todayStr = Utilities.formatDate(new Date(), "Asia/Taipei", "yyyy-MM-dd");

  const shockAlerts = [];
  const allTrusted = [];

  CONFIG.WATCHLIST.forEach(item => {
    const rssItems = fetchGoogleNewsRss_(item.query, item.lang);
    rssItems.forEach(news => {
      const tier = classifyTier_(news.source, news.title);
      if (!tier) return;

      const matchedKeywords = findShockKeywords_(news.title);
      const dedupeKey = Utilities.base64EncodeWebSafe(news.title.slice(0, 80));
      const isNew = !seenMap[dedupeKey];

      const record = {
        label: item.label,
        spilloverTW: item.spilloverTW,
        tier: tier,
        title: news.title,
        source: news.source,
        pubDate: news.pubDate,
        link: news.link,
        keywords: matchedKeywords,
        isNew: isNew
      };

      allTrusted.push(record);
      if (isNew && matchedKeywords.length > 0) {
        shockAlerts.push(record);
        seenMap[dedupeKey] = todayStr;
      }
    });
  });

  // 保留最近 200 筆去重紀錄避免 Properties 過大
  const keys = Object.keys(seenMap);
  if (keys.length > 200) {
    keys.slice(0, keys.length - 200).forEach(k => delete seenMap[k]);
  }
  props.setProperty("SEEN_NEWS_KEYS", JSON.stringify(seenMap));

  // 寫入 Google Drive 共用資料夾（供 3 個 Gemini Pro 帳號 + Claude + NotebookLM 讀取）
  if (allTrusted.length > 0) {
    writeBriefToDrive_(todayStr, nowStr, allTrusted, shockAlerts);
  }

  // 只有偵測到「新」高衝擊事件才發即時警示信，避免日常雜訊洗版
  if (shockAlerts.length > 0) {
    sendShockEmail_(nowStr, shockAlerts);
  }
}

function fetchGoogleNewsRss_(query, lang) {
  const q = encodeURIComponent(query);
  const url = lang === "zh-TW"
    ? `https://news.google.com/rss/search?q=${q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant`
    : `https://news.google.com/rss/search?q=${q}&hl=en-US&gl=US&ceid=US:en`;

  const resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) return [];

  const doc = XmlService.parse(resp.getContentText());
  const channel = doc.getRootElement().getChild("channel");
  if (!channel) return [];

  const entries = channel.getChildren("item").slice(0, 12);
  return entries.map(el => {
    const title = el.getChildText("title") || "";
    const link = el.getChildText("link") || "";
    const pubDate = el.getChildText("pubDate") || "";
    const sourceEl = el.getChild("source");
    const source = sourceEl ? sourceEl.getText() : (title.includes(" - ") ? title.split(" - ").pop() : "");
    return { title, link, pubDate, source };
  });
}

function classifyTier_(source, title) {
  const hay = `${source} ${title}`.toLowerCase();
  if (CONFIG.TIER1_SOURCES.some(s => hay.includes(s))) return "TIER_1";
  if (CONFIG.TIER2_SOURCES.some(s => hay.includes(s))) return "TIER_2";
  return null;
}

function findShockKeywords_(title) {
  const lower = title.toLowerCase();
  return CONFIG.SHOCK_KEYWORDS.filter(kw => lower.includes(kw));
}

function getOrCreateFolder_(parent, name) {
  const iter = parent.getFoldersByName(name);
  return iter.hasNext() ? iter.next() : parent.createFolder(name);
}

function writeBriefToDrive_(todayStr, nowStr, allTrusted, shockAlerts) {
  const rootFolder = getOrCreateFolder_(DriveApp, CONFIG.ROOT_FOLDER_NAME);
  const inboxFolder = getOrCreateFolder_(rootFolder, CONFIG.INBOX_SUBFOLDER_NAME);
  const fileName = `${todayStr}_Trusted_News_Feed.md`;

  const lines = [
    `# 可信新聞事件雷達（更新時間：${nowStr} 台北時間）`,
    "",
    `- **高衝擊事件警示數**：${shockAlerts.length} 筆（若 > 0 請立即切換為 Event Shock 動態權重：新聞 40% / 技術 35% / 財報 15%）`,
    `- **可信來源新聞總數**：${allTrusted.length} 筆（僅保留 Tier-1 / Tier-2 媒體）`,
    "",
    "## 高衝擊事件快報（需交由 Gemini Pro #1 與 #2 優先判讀）"
  ];

  if (shockAlerts.length === 0) {
    lines.push("- 目前無新增高衝擊關鍵字新聞。");
  } else {
    shockAlerts.forEach((a, i) => {
      lines.push(`${i + 1}. **[${a.tier}] [${a.label}]** ${a.title}`);
      lines.push(`   - 命中關鍵字：\`${a.keywords.join(", ")}\` ｜ 台股連動關注：\`${a.spilloverTW}\``);
      lines.push(`   - 來源：${a.source} ｜ 連結：${a.link}`);
    });
  }

  lines.push("", "## 今日可信新聞全清單");
  allTrusted.slice(0, 20).forEach((n, i) => {
    lines.push(`${i + 1}. **[${n.tier}]** (${n.label}) ${n.title} — _${n.source}_`);
  });

  const content = lines.join("\n");
  const existing = inboxFolder.getFilesByName(fileName);
  if (existing.hasNext()) {
    existing.next().setContent(content);
  } else {
    inboxFolder.createFile(fileName, content, MimeType.PLAIN_TEXT);
  }
}

function sendShockEmail_(nowStr, shockAlerts) {
  const recipient = CONFIG.ALERT_EMAIL || Session.getActiveUser().getEmail();
  if (!recipient) return;

  const subject = `[Red-Team 事件警示] 偵測到 ${shockAlerts.length} 則可信高衝擊新聞（${nowStr}）`;
  const bodyLines = [
    `Investment Red-Team 可信新聞雷達偵測到高衝擊事件（${nowStr}）：`,
    "提醒：事件衝擊期間請將財報權重降至 15%，先交由 Gemini #1（事件定性）與 Gemini #2（技術線型）判讀，勿盲目左側接刀！",
    ""
  ];
  shockAlerts.forEach((a, idx) => {
    bodyLines.push(`${idx + 1}. [${a.tier}] ${a.label}：${a.title}`);
    bodyLines.push(`   關鍵字：${a.keywords.join(", ")} ｜ 台股供應鏈留意：${a.spilloverTW}`);
    bodyLines.push(`   來源：${a.source}`);
    bodyLines.push(`   連結：${a.link}`, "");
  });
  bodyLines.push("完整檔案已同步至 Google 雲端硬碟：Investment-Red-Team-Hub / 00_Inbox_Context");

  GmailApp.sendEmail(recipient, subject, bodyLines.join("\n"));
}
