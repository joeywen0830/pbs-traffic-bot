import os
import time
import json
import urllib.parse
import requests
from bs4 import BeautifulSoup

# ==================== 設定區 ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 關鍵字過濾清單
KEYWORDS = ["火燒", "起火", "火警", "火災", "煙", "翻"]
PBS_URL = "https://rtr.pbs.gov.tw/pbsmgt/RoadAll.html"

MAX_PUSH_COUNT = 5          # 每個事故最多推播 5 次
PUSH_INTERVAL_SEC = 115     # 間隔約 2 分鐘（以 115 秒防止微小誤差）
STATE_FILE = "state.json"   # 儲存推播進度與記憶的檔案
# ===============================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"tracking": {}, "completed": []}

def save_state(state):
    # 保留最新 300 筆已完結紀錄，防止檔案過大
    state["completed"] = state["completed"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_pbs_data():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    resp = requests.get(PBS_URL, headers=headers, timeout=25)
    resp.encoding = "big5"  # 警廣網站主要為 Big5 編碼
    return resp.text

def parse_pbs_table(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    records = []

    for tr in rows:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 4:
            continue
        if any(h in tds for h in ["項次", "更新時間", "通報時間"]):
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
    query_str = f"{item['region']} {item['road']} {item['direction']}".strip()
    encoded = urllib.parse.quote(query_str or f"{item['road']} 事故")
    return {
        "google": f"https://www.google.com/maps/search/?api=1&query={encoded}",
        "apple": f"https://maps.apple.com/?q={encoded}"
    }

def send_telegram_alert(item, count, map_links):
    title = "🚨 <b>警廣異常路況警報 [第 1/5 次通報]</b>" if count == 1 else f"⚠️ <b>警廣路況持續追蹤 [第 {count}/5 次提醒]</b>"
    full_text = f"{item['road']} {item['direction']} {item['comment']}".strip()

    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 <b>區域/路段</b>：{item['region']} {item['road']} {item['direction']}\n"
        f"⏱ <b>通報時間</b>：{item['time']}\n"
        f"📝 <b>事件內容</b>：\n{full_text}\n"
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
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"發送 Telegram 失敗: {e}")

def main():
    state = load_state()
    tracking = state.get("tracking", {})
    completed_set = set(state.get("completed", []))
    now = time.time()

    print("▶ 開始連線警廣抓取路況...")
    try:
        html = fetch_pbs_data()
        records = parse_pbs_table(html)
        print(f"成功解析出 {len(records)} 筆即時路況。")
    except Exception as e:
        print(f"抓取警廣失敗: {e}")
        return

    # 1. 檢查關鍵字與首次推播（第 1 次）
    for item in records:
        full_text = f"{item['road']} {item['direction']} {item['comment']}".strip()
        event_id = f"{item['time']}_{item['road']}_{item['comment'][:25]}"

        if event_id in completed_set:
            continue

        if any(kw in full_text for kw in KEYWORDS):
            if event_id not in tracking:
                print(f"🚨 發現符合事故，發送第 1 次通報: {full_text}")
                map_links = build_map_urls(item)
                send_telegram_alert(item, 1, map_links)
                tracking[event_id] = {
                    "item": item,
                    "push_count": 1,
                    "last_pushed_at": now,
                    "map_links": map_links
                }

    # 2. 檢查追蹤中的事故推播（第 2 ~ 5 次）
    events_to_delete = []
    for event_id, ev in tracking.items():
        if ev["push_count"] < MAX_PUSH_COUNT and (now - ev["last_pushed_at"]) >= PUSH_INTERVAL_SEC:
            ev["push_count"] += 1
            ev["last_pushed_at"] = now
            print(f"⚠️ 發送第 {ev['push_count']}/5 次提醒: {ev['item']['road']}")
            send_telegram_alert(ev["item"], ev["push_count"], ev["map_links"])

        if ev["push_count"] >= MAX_PUSH_COUNT:
            print(f"✔ 事故推播滿 5 次，結案: {ev['item']['road']}")
            completed_set.add(event_id)
            events_to_delete.append(event_id)

    for event_id in events_to_delete:
        del tracking[event_id]

    state["tracking"] = tracking
    state["completed"] = list(completed_set)
    save_state(state)
    print("✔ 檢查完成並成功保存進度狀態。")

if __name__ == "__main__":
    main()
