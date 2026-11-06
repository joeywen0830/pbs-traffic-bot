import os
import sys
import time
import json
import urllib.parse
import requests
from bs4 import BeautifulSoup

# ==================== 自訂參數設定 ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# 關鍵字過濾清單
KEYWORDS = ["火燒", "起火", "火警", "火災", "煙", "翻"]
PBS_URL = "https://rtr.pbs.gov.tw/pbsmgt/RoadAll.html"

MAX_PUSH_COUNT = 5          # 每個事故最多推播 5 次
PUSH_INTERVAL_SEC = 115     # 間隔約 2 分鐘重推（以 115 秒防計時器誤差）
STATE_FILE = "state.json"   # 儲存推播進度與記憶的檔案

TOTAL_CYCLES = 5            # 單次 Workflow 執行 5 次
INTERVAL_SECONDS = 60       # 每次間隔 60 秒（達成每分鐘巡檢）
# =====================================================

def load_state():
    """載入歷史推播與追蹤狀態"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"讀取 state.json 發生錯誤，初始化狀態: {e}")
    return {"tracking": {}, "completed": []}

def save_state(state):
    """保存狀態到 state.json，保留最新 300 筆已完結事件防檔案膨脹"""
    state["completed"] = state.get("completed", [])[-300:]
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入 state.json 失敗: {e}")

def fetch_pbs_data():
    """向警廣伺服器抓取即時路況網頁內容"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    resp = requests.get(PBS_URL, headers=headers, timeout=20)
    # 警廣主要為 Big5 編碼
    try:
        return resp.content.decode("big5")
    except UnicodeDecodeError:
        return resp.content.decode("utf-8", errors="ignore")

def parse_pbs_table(html):
    """解析 HTML 表格為結構化路況記錄"""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    records = []

    for tr in rows:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 4:
            continue
        # 排除表頭標題列
        if any(h in tds for h in ["項次", "更新時間", "通報時間", "路況"]):
            continue

        comment = max(tds, key=len)
        others = [x for x in tds if x != comment]
        time_text = next((x for x in others if ":" in x), "即時通報")
        remaining = [x for x in others if x != time_text]

        region = remaining[0] if len(remaining) > 0 else ""
        road = remaining[1] if len(remaining) > 1 else ""
        direction = remaining[2] if len(remaining) > 2 else ""

        records.append({
            "time": time_text,
            "region": region,
            "road": road,
            "direction": direction,
            "comment": comment
        })
    return records

def build_map_urls(item):
    """組裝 Google Maps 與 Apple Maps 導航連結"""
    query_str = f"{item['region']} {item['road']} {item['direction']}".strip()
    encoded = urllib.parse.quote(query_str or f"{item['road']} 事故")
    return {
        "google": f"https://www.google.com/maps/search/?api=1&query={encoded}",
        "apple": f"https://maps.apple.com/?q={encoded}"
    }

def escape_html(text):
    """跳脫 HTML 特殊符號防止 Telegram 解析錯誤"""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_telegram_alert(item, count, map_links):
    """發送 Telegram HTML 格式告警通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，略過推播。")
        return

    title = "🚨 <b>警廣異常路況警報 [第 1/5 次通報]</b>" if count == 1 else f"⚠️ <b>警廣路況持續追蹤 [第 {count}/5 次提醒]</b>"
    full_text = f"{item['road']} {item['direction']} {item['comment']}".strip()

    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 <b>區域/路段</b>：{escape_html(item['region'])} {escape_html(item['road'])} {escape_html(item['direction'])}\n"
        f"⏱ <b>通報時間</b>：{escape_html(item['time'])}\n"
        f"📝 <b>事件內容</b>：\n{escape_html(full_text)}\n"
        f"🗺 <b>地圖導航</b>：<a href=\"{map_links['google']}\">Google 地圖</a> ｜ <a href=\"{map_links['apple']}\">Apple Maps</a>\n"
        f"━━━━━━━━━━━━━━━"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram 回傳異常: {resp.text}")
    except Exception as e:
        print(f"發送 Telegram 失敗: {e}")

def run_single_check(state):
    """單次巡檢處理流程"""
    tracking = state.get("tracking", {})
    completed_set = set(state.get("completed", []))
    now = time.time()

    try:
        html = fetch_pbs_data()
        records = parse_pbs_table(html)
        print(f"成功取得警廣網頁，解析出 {len(records)} 筆路況。")
    except Exception as e:
        print(f"抓取警廣資料失敗: {e}")
        return

    # 1. 檢查關鍵字與初次發報（第 1 次）
    for item in records:
        full_text = f"{item['road']} {item['direction']} {item['comment']}".strip()
        event_id = f"{item['time']}_{item['road']}_{item['comment'][:25]}"

        # 已推播滿 5 次結案者不再處理
        if event_id in completed_set:
            continue

        if any(kw in full_text for kw in KEYWORDS):
            if event_id not in tracking:
                print(f"🚨 命中關鍵字，發送第 1 次推播: {full_text}")
                map_links = build_map_urls(item)
                send_telegram_alert(item, 1, map_links)
                tracking[event_id] = {
                    "item": item,
                    "push_count": 1,
                    "last_pushed_at": now,
                    "map_links": map_links
                }

    # 2. 檢查追蹤池中事件（每 2 分鐘重推第 2 ~ 5 次）
    events_to_delete = []
    for event_id, ev in tracking.items():
        if ev["push_count"] < MAX_PUSH_COUNT and (now - ev["last_pushed_at"]) >= PUSH_INTERVAL_SEC:
            ev["push_count"] += 1
            ev["last_pushed_at"] = now
            print(f"⚠️ 發送第 {ev['push_count']}/5 次提醒: {ev['item']['road']}")
            send_telegram_alert(ev["item"], ev["push_count"], ev["map_links"])

        if ev["push_count"] >= MAX_PUSH_COUNT:
            print(f"✔ 事故推播滿 5 次，結案移入封存區: {ev['item']['road']}")
            completed_set.add(event_id)
            events_to_delete.append(event_id)

    for event_id in events_to_delete:
        del tracking[event_id]

    state["tracking"] = tracking
    state["completed"] = list(completed_set)
    save_state(state)

def main():
    state = load_state()
    print(f"🚀 啟動高頻輪詢模式：每 {INTERVAL_SECONDS} 秒巡檢一次，共執行 {TOTAL_CYCLES} 次")

    for cycle in range(TOTAL_CYCLES):
        current_time_str = time.strftime("%H:%M:%S", time.localtime())
        print(f"\n--- [{current_time_str}] 執行第 {cycle + 1}/{TOTAL_CYCLES} 次巡檢 ---")
        
        run_single_check(state)

        # 最後一次檢查完畢直接結束，無縫交接給下一次排程
        if cycle < TOTAL_CYCLES - 1:
            time.sleep(INTERVAL_SECONDS)

    print("\n✅ 本輪 5 次巡檢全數完成。")

if __name__ == "__main__":
    main()
