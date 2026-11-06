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

# 關鍵字清單
KEYWORDS = ["火燒", "起火", "火警", "火災", "煙", "翻"]

# 警廣原始網址
RAW_PBS_URL = "https://rtr.pbs.gov.tw/pbsmgt/RoadAll.html"

# 透過 Jina Reader 免登入中繼代理（專門穿透政府海外 Geo-blocking）
PROXY_PBS_URL = f"https://r.jina.ai/{RAW_PBS_URL}"

MAX_PUSH_COUNT = 5          # 每個事故最多推播 5 次
PUSH_INTERVAL_SEC = 115     # 間隔約 2 分鐘重推
STATE_FILE = "state.json"   # 儲存追蹤狀態檔案

TOTAL_CYCLES = 5            # 單次排程執行 5 輪
INTERVAL_SECONDS = 60       # 每輪間隔 60 秒（達成每分鐘巡檢）
# ====================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"tracking": {}, "completed": []}

def save_state(state):
    state["completed"] = state.get("completed", [])[-300:]
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入 state.json 失敗: {e}")

def fetch_pbs_data():
    """透過中繼網關抓取警廣網頁"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Return-Format": "markdown"
    }

    # 1. 優先透過 Jina 代理通道穿透警廣防火牆
    try:
        resp = requests.get(PROXY_PBS_URL, headers=headers, timeout=25)
        if resp.status_code == 200 and len(resp.text) > 200:
            return resp.text
    except Exception as e:
        print(f"中繼通道連線異常: {e}，嘗試直接連線備援...")

    # 2. 直接連線備援
    resp = requests.get(RAW_PBS_URL, timeout=15, verify=False)
    if resp.status_code == 200:
        return resp.content.decode("big5", errors="ignore")

    raise Exception("所有連線管道皆無法取得路況資料。")

def parse_traffic_content(text):
    """解析網頁或 Markdown 格式路況"""
    records = []
    lines = text.split("\n")

    current_item = {}
    for line in lines:
        line = line.strip()
        if not line or "項次" in line:
            continue

        # 匹配常見的警廣路況格式（包含時間、道路名稱與描述）
        if any(h in line for h in ["國道", "台61", "台74", "市區", "東向", "西向", "南下", "北上"]) or len(line) > 15:
            # 擷取可能包含的時間
            time_part = "即時通報"
            for part in line.split():
                if ":" in part and len(part) <= 8:
                    time_part = part
                    break

            records.append({
                "time": time_part,
                "region": "",
                "road": line[:20],
                "direction": "",
                "comment": line
            })

    return records

def build_map_urls(item):
    encoded = urllib.parse.quote(item['comment'][:30] or "即時路況事故")
    return {
        "google": f"https://www.google.com/maps/search/?api=1&query={encoded}",
        "apple": f"https://maps.apple.com/?q={encoded}"
    }

def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_telegram_alert(item, count, map_links):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("未設定 TELEGRAM 金鑰，略過推播。")
        return

    title = "🚨 <b>警廣異常路況警報 [第 1/5 次通報]</b>" if count == 1 else f"⚠️ <b>警廣路況持續追蹤 [第 {count}/5 次提醒]</b>"
    full_text = item['comment'].strip()

    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━\n"
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
    tracking = state.get("tracking", {})
    completed_set = set(state.get("completed", []))
    now = time.time()

    try:
        content = fetch_pbs_data()
        records = parse_traffic_content(content)
        print(f"成功連通並取得路況資料！共解析出 {len(records)} 筆片段。")
    except Exception as e:
        print(f"抓取警廣資料失敗: {e}")
        return

    # 1. 關鍵字比對與第 1 次推播
    for item in records:
        full_text = item['comment']
        event_id = f"{item['time']}_{full_text[:30]}"

        if event_id in completed_set:
            continue

        if any(kw in full_text for kw in KEYWORDS):
            if event_id not in tracking:
                print(f"🚨 命中關鍵字，發送第 1 次推播: {full_text[:40]}")
                map_links = build_map_urls(item)
                send_telegram_alert(item, 1, map_links)
                tracking[event_id] = {
                    "item": item,
                    "push_count": 1,
                    "last_pushed_at": now,
                    "map_links": map_links
                }

    # 2. 每 2 分鐘重推（第 2 至 5 次）
    events_to_delete = []
    for event_id, ev in tracking.items():
        if ev["push_count"] < MAX_PUSH_COUNT and (now - ev["last_pushed_at"]) >= PUSH_INTERVAL_SEC:
            ev["push_count"] += 1
            ev["last_pushed_at"] = now
            print(f"⚠️ 發送第 {ev['push_count']}/5 次提醒: {ev['item']['comment'][:30]}")
            send_telegram_alert(ev["item"], ev["push_count"], ev["map_links"])

        if ev["push_count"] >= MAX_PUSH_COUNT:
            print(f"✔ 事故推播滿 5 次，結案: {ev['item']['comment'][:30]}")
            completed_set.add(event_id)
            events_to_delete.append(event_id)

    for event_id in events_to_delete:
        del tracking[event_id]

    state["tracking"] = tracking
    state["completed"] = list(completed_set)
    save_state(state)

def main():
    state = load_state()
    print(f"🚀 啟動高頻輪詢：每 {INTERVAL_SECONDS} 秒巡檢一次，共執行 {TOTAL_CYCLES} 次")

    for cycle in range(TOTAL_CYCLES):
        current_time_str = time.strftime("%H:%M:%S", time.localtime())
        print(f"\n--- [{current_time_str}] 執行第 {cycle + 1}/{TOTAL_CYCLES} 次巡檢 ---")
        run_single_check(state)

        if cycle < TOTAL_CYCLES - 1:
            time.sleep(INTERVAL_SECONDS)

    print("\n✅ 本輪巡檢完成。")

if __name__ == "__main__":
    main()
