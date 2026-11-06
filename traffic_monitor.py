import os
import sys
import time
import json
import urllib.parse
import urllib3
import requests

# 關閉台灣政府 SSL 憑證可能的警告訊息
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 1. 自訂參數設定 ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# 關鍵字過濾清單
KEYWORDS = ["火燒", "起火", "火警", "火災", "煙", "翻"]

# 政府資料開放平臺：警廣即時路況官方公開 JSON 下載端點 (資料集代碼 15221)
DATA_GOV_JSON_URL = "https://data.moi.gov.tw/MoiOD/System/DownloadFile.aspx?DATA=E88E4B07-7429-41E6-A060-CE0BCF183C66"

MAX_PUSH_COUNT = 5          # 每個事故最多推播 5 次
PUSH_INTERVAL_SEC = 115     # 間隔約 2 分鐘重推（以 115 秒防止微小計時差）
STATE_FILE = "state.json"   # 儲存推播進度檔案

TOTAL_CYCLES = 5            # 單次排程執行 5 次
INTERVAL_SECONDS = 60       # 每次間隔 60 秒（達成每分鐘巡檢）
# ========================================================

def load_state():
    """載入歷史推播與追蹤記錄"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"讀取 state.json 發生異常，初始化狀態: {e}")
    return {"tracking": {}, "completed": []}

def save_state(state):
    """回存狀態至 state.json，保留最新 400 筆已結案事件避免檔案過大"""
    state["completed"] = state.get("completed", [])[-400:]
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入 state.json 失敗: {e}")

def fetch_traffic_json():
    """向政府開放資料平臺下載即時路況 JSON"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    resp = requests.get(DATA_GOV_JSON_URL, headers=headers, timeout=25, verify=False)
    if resp.status_code != 200:
        raise Exception(f"下載失敗，HTTP 狀態碼: {resp.status_code}")

    # 以 utf-8-sig 解析避免微軟 UTF-8 BOM 造成 JSON 報錯
    content = resp.content.decode("utf-8-sig", errors="ignore")
    data = json.loads(content)

    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        return data.get("records") or data.get("result") or data.get("data") or []
    return []

def build_map_urls(item):
    """利用官方提供的 posx/posy 座標產生高精準度導航連結"""
    raw_lon = item.get("posx") or item.get("PosX") or item.get("lon") or 0
    raw_lat = item.get("posy") or item.get("PosY") or item.get("lat") or 0

    region = item.get("region") or item.get("Region") or ""
    road = item.get("road") or item.get("Road") or ""
    direction = item.get("direction") or item.get("Direction") or ""
    comment = item.get("comment") or item.get("Comment") or ""

    query_label = f"{region} {road} {direction}".strip() or "事故現場"

    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
    except (ValueError, TypeError):
        lat, lon = 0.0, 0.0

    # 驗證座標是否在台灣合理範圍（緯度 21~26、經度 119~123）
    if 21.0 < lat < 26.0 and 119.0 < lon < 123.0:
        google_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        apple_url = f"https://maps.apple.com/?ll={lat},{lon}&q={urllib.parse.quote(query_label)}"
    else:
        encoded = urllib.parse.quote(query_label or comment[:25])
        google_url = f"https://www.google.com/maps/search/?api=1&query={encoded}"
        apple_url = f"https://maps.apple.com/?q={encoded}"

    return {"google": google_url, "apple": apple_url}

def escape_html(text):
    """跳脫 HTML 保留字元防止 Telegram 解析崩潰"""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_telegram_alert(item, count, map_links):
    """發送 Telegram HTML 告警通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，略過推播。")
        return

    title = "🚨 <b>警廣異常路況警報 [第 1/5 次通報]</b>" if count == 1 else f"⚠️ <b>警廣路況持續追蹤 [第 {count}/5 次提醒]</b>"

    region = item.get("region") or item.get("Region") or ""
    road = item.get("road") or item.get("Road") or ""
    direction = item.get("direction") or item.get("Direction") or ""
    time_str = item.get("happentime") or item.get("HappenTime") or item.get("haptime") or item.get("modtime") or "即時通報"
    comment = item.get("comment") or item.get("Comment") or item.get("desc") or ""

    location_text = f"{region} {road} {direction}".strip()

    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 <b>區域/路段</b>：{escape_html(location_text)}\n"
        f"⏱ <b>通報時間</b>：{escape_html(time_str)}\n"
        f"📝 <b>事件內容</b>：\n{escape_html(comment)}\n"
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
        records = fetch_traffic_json()
        print(f"成功取得政府開放資料！共解析出 {len(records)} 筆即時路況。")
    except Exception as e:
        print(f"抓取政府開放資料失敗: {e}")
        return

    # 1. 關鍵字比對與首次推播（第 1 次）
    for item in records:
        comment = str(item.get("comment") or item.get("Comment") or item.get("desc") or "")
        road = str(item.get("road") or item.get("Road") or "")
        region = str(item.get("region") or item.get("Region") or "")
        direction = str(item.get("direction") or item.get("Direction") or "")
        time_str = str(item.get("happentime") or item.get("HappenTime") or item.get("haptime") or "")

        full_text = f"{region} {road} {direction} {comment}".strip()

        # 優先使用官方 UID，無則以時間與內容雜湊作為 ID
        event_id = str(item.get("UID") or item.get("uid") or f"{time_str}_{road}_{comment[:25]}")

        # 已推播滿 5 次者直接略過
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

    # 2. 檢查追蹤池中事件（每 2 分鐘重推第 2 ~ 5 次）
    events_to_delete = []
    for event_id, ev in tracking.items():
        if ev["push_count"] < MAX_PUSH_COUNT and (now - ev["last_pushed_at"]) >= PUSH_INTERVAL_SEC:
            ev["push_count"] += 1
            ev["last_pushed_at"] = now
            road_name = ev["item"].get("road") or ev["item"].get("Road") or "未知路段"
            print(f"⚠️ 發送第 {ev['push_count']}/5 次提醒: {road_name}")
            send_telegram_alert(ev["item"], ev["push_count"], ev["map_links"])

        if ev["push_count"] >= MAX_PUSH_COUNT:
            road_name = ev["item"].get("road") or ev["item"].get("Road") or "未知路段"
            print(f"✔ 事故推播滿 5 次，結案移入封存區: {road_name}")
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

        # 最後一次檢查完成後直接結束，交接給 GitHub Actions 保存狀態
        if cycle < TOTAL_CYCLES - 1:
            time.sleep(INTERVAL_SECONDS)

    print("\n✅ 本輪巡檢完成。")

if __name__ == "__main__":
    main()
