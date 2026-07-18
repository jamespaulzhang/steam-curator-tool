#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam Curator 自动化工具（完整增强版 + GitHub Actions 兼容）
- 抓取官方邮箱（Failed/NaN 区分）
- 判断免费 / 下架 / 拥有
- 支持断点续传、升序抓取、Failed 重试、状态补全、指定起点
- 适配 GitHub Actions：环境变量、命令行模式、超时控制、信号处理
- 定期保存 + 原子写入，确保数据不丢失
"""

import requests
from bs4 import BeautifulSoup
import re
import csv
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import time
import sys
import os
import json
import argparse
import warnings
import signal

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+")

# ========================= 配置 =========================
# 敏感信息优先从环境变量读取，方便 GitHub Actions 使用；本地运行时使用默认值
GMAIL_USER = os.environ.get("GMAIL_USER", "gameutopiacurator@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "rulsmdrmthdlfpjj")

EMAIL_SUBJECT = "Collaboration Proposal from Steam Curator ‘Game Utopia’"
EMAIL_BODY_TEMPLATE = """Dear Developer/Publisher,

I hope this message finds you well.

I am the administrator of the Steam Curator group “Game Utopia”, which currently has over 13,200 followers and has reviewed more than 1200 games. Our mission is to help players discover their next gaming adventure, focusing on showcasing quality games and sharing unique experiences. Whether you’re a hardcore gamer or a casual enthusiast, you’ll find something meaningful with us.

In Game Utopia, we review games of all types and scales:
• Hidden Indie Gems: Uncovering creative and brilliant works that deserve more attention;
• AAA Blockbusters: Analyzing top-tier productions to reveal their strengths and flaws;
• Adult games: We have many experienced writer which they already test and review hundreds of adult games and promote in our community

Considering your recent game release or promotional activities, I would like to propose a collaboration opportunity. Through my platform, I can offer the following support for your game:
• Publish detailed reviews or recommendations as soon as possible;
• Provide targeted recommendations to relevant player groups;
• Promote your game across our Steam community and also in our 2000 fans QQ group: 937099770.

If you are interested in collaborating, I would greatly appreciate it if you could provide 3 curator connection copies or review keys for **{game_name}**, or any other support. We can discuss further details and explore how to bring a better gaming experience to players together.

I look forward to your response!

Best regards,
Yuxiang
Steam Curator “Game Utopia”

Personal Email: jamespaulzhang@gmail.com
Discord: https://discordapp.com/users/365125763478847500

Game Utopia curator page: https://store.steampowered.com/curator/45337284/

This is our traffic stats:

My steam profile page: https://steamcommunity.com/id/viestbelle/
"""

# 数据源
GITHUB_JSON_URL = "https://raw.githubusercontent.com/jsnli/steamappidlist/master/data/games_appid.json"
OUTPUT_CSV = "steam_emails.csv"

# 过滤：只处理 AppID >= 此值的游戏（0 表示全部）
MIN_APPID = 0

# 请求延迟（秒）
DELAY_BETWEEN_APPIDS = 0.3          # 邮箱抓取速度
DELAY_BETWEEN_EMAILS = 5            # 发件间隔
STATUS_DELAY = 0                    # 状态补充专用延迟

# 测试模式
DRY_RUN = False

# ======== 拥有游戏 & API Key ========
OWNED_APPS_FILE = "owned_appids.txt"
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "578A56730A0159A5AF01CEA6B9075902")
STEAM_ID = os.environ.get("STEAM_ID", "76561198368348725")
OWNED_CACHE_FILE = "owned_cache.json"

# ======== 下架游戏列表（增加重试和更长的缓存） ========
DELISTED_API_URL = "https://steam-tracker.com/api?action=GetAppListV3"
DELISTED_CACHE_FILE = "delisted_cache.json"
DELISTED_CACHE_MAX_AGE = 259200    # 缓存有效期：3天（秒）

# ======== 超时控制（仅自动模式生效） ========
MAX_RUNTIME = 5.9 * 3600   # 5.5 小时，与 workflow 的 timeout 匹配

# ======== 定期保存间隔（处理多少个游戏后自动保存） ========
SAVE_INTERVAL = 100

# ======== 全局变量（用于信号处理） ========
_current_results = None
_current_auto_mode = False

# ========================= 信号处理 =========================
def graceful_exit(signum, frame):
    print("\n⏰ 收到终止信号，正在保存数据...")
    if _current_results is not None:
        save_csv_atomic(_current_results)
        print("数据已保存。")
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_exit)

# ========================= 工具函数 =========================
def load_games_from_github():
    """下载 games_appid.json，返回 {appid: name}"""
    print("正在从 GitHub 下载 Steam 游戏列表...")
    try:
        resp = requests.get(GITHUB_JSON_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        apps = []
        if "applist" in data and "apps" in data["applist"]:
            apps = data["applist"]["apps"]
        elif isinstance(data, list):
            apps = data
        else:
            print("❌ JSON 格式无法解析")
            return {}
        appid_name_map = {}
        filtered = 0
        for app in apps:
            aid = app.get("appid")
            name = app.get("name", "")
            if not aid:
                continue
            if MIN_APPID > 0 and int(aid) < MIN_APPID:
                filtered += 1
                continue
            appid_name_map[str(aid)] = name
        print(f"✅ 加载 {len(appid_name_map)} 个游戏（已过滤 {filtered} 个旧游戏）")
        return appid_name_map
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        return {}

def get_support_email(appid):
    """
    提取官方联系邮箱，失败区分网络错误和真实无邮箱：
    - 网络异常 / 非200状态码 -> "Failed"
    - 成功但没有邮箱 -> "NaN"
    """
    url = f"https://help.steampowered.com/zh-cn/wizard/HelpWithGameTechnicalIssue?appid={appid}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, timeout=15, headers=headers)
        if resp.status_code != 200:
            return "Failed"
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select(".help_official_support_row")
        for row in rows:
            text = row.get_text().strip()
            if "电子邮件" in text or "Email" in text:
                link = row.find("a")
                if link and link.get("href"):
                    href = link["href"]
                    if href.startswith("mailto:"):
                        return href[len("mailto:"):]
                    if "@" in href:
                        return href
                match = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text)
                if match:
                    return match.group(0)
        return "NaN"
    except Exception:
        return "Failed"

def get_game_details(appid):
    """获取游戏详情，返回 (is_free, success) 或 (None, False)"""
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
    headers = {"User-Agent": "Mozilla/5.0"}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=15, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                app_data = data.get(str(appid))
                if app_data and app_data.get("success"):
                    is_free = app_data["data"].get("is_free", False)
                    return is_free, True
                else:
                    return None, False
            elif resp.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"  [!] 429 Too Many Requests，等待 {wait}s...")
                time.sleep(wait)
                continue
            elif resp.status_code == 403:
                print(f"  [!] 403 Forbidden，等待 5 分钟...")
                time.sleep(300)
                continue
            else:
                print(f"  [!] 未知状态码 {resp.status_code}")
                return None, False
        except Exception as e:
            print(f"  [!] 请求 appdetails 失败: {e}")
            time.sleep(2)
    return None, False

def load_delisted_apps():
    """
    加载下架游戏列表，返回 set of appid。
    优先使用缓存（3天内有效），失败时自动重试最多3次。
    """
    if os.path.exists(DELISTED_CACHE_FILE):
        try:
            with open(DELISTED_CACHE_FILE, "r") as f:
                cache = json.load(f)
                if time.time() - cache.get("timestamp", 0) < DELISTED_CACHE_MAX_AGE:
                    print(f"从缓存加载下架游戏列表，共 {len(cache['ids'])} 个")
                    return set(cache["ids"])
                else:
                    print("下架游戏缓存已过期，重新获取...")
        except:
            pass

    print("正在从 steam-tracker 获取下架游戏列表...")
    for attempt in range(1, 4):
        try:
            resp = requests.get(DELISTED_API_URL, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                removed = data.get("removed_apps", [])
                ids = set()
                for app in removed:
                    if isinstance(app, dict):
                        ids.add(str(app.get("appid")))
                    else:
                        ids.add(str(app))
                with open(DELISTED_CACHE_FILE, "w") as f:
                    json.dump({"timestamp": time.time(), "ids": list(ids)}, f)
                print(f"✅ 下架游戏列表已更新，共 {len(ids)} 个")
                return ids
            else:
                print(f"⚠️ 获取失败，状态码 {resp.status_code}，尝试 {attempt}/3")
        except Exception as e:
            print(f"❌ 请求异常: {e}，尝试 {attempt}/3")
        if attempt < 3:
            time.sleep(10)

    print("⚠️ 多次重试后仍无法获取下架数据，下架状态将全部标记为 False")
    return set()

def load_owned_apps():
    """加载拥有的游戏 AppID 集合"""
    if os.path.exists(OWNED_APPS_FILE):
        with open(OWNED_APPS_FILE, "r") as f:
            ids = {line.strip() for line in f if line.strip().isdigit()}
        print(f"从 {OWNED_APPS_FILE} 加载 {len(ids)} 个拥有游戏")
        return ids

    if STEAM_API_KEY and STEAM_ID:
        print("尝试自动获取拥有游戏列表...")
        if os.path.exists(OWNED_CACHE_FILE):
            try:
                with open(OWNED_CACHE_FILE, "r") as f:
                    cache = json.load(f)
                if time.time() - cache.get("timestamp", 0) < 86400:
                    print("使用缓存的拥有游戏列表")
                    return set(map(str, cache["ids"]))
            except:
                pass

        url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
        params = {
            "key": STEAM_API_KEY,
            "steamid": STEAM_ID,
            "format": "json",
            "include_appinfo": 0,
            "include_played_free_games": 0
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                games = data.get("response", {}).get("games", [])
                ids = {str(g["appid"]) for g in games}
                with open(OWNED_CACHE_FILE, "w") as f:
                    json.dump({"timestamp": time.time(), "ids": list(ids)}, f)
                print(f"✅ 自动获取 {len(ids)} 个拥有游戏")
                return ids
            else:
                print(f"⚠️ API 请求失败，状态码 {resp.status_code}")
        except Exception as e:
            print(f"❌ 自动获取失败: {e}")

    print("未配置拥有游戏来源，所有游戏将标记为未拥有")
    return set()

def send_email(to_addr, game_name):
    """发送邮件"""
    if DRY_RUN:
        print(f"  [测试] 未发邮件给 {to_addr}")
        return True
    msg = MIMEText(EMAIL_BODY_TEMPLATE.format(game_name=game_name or "your game"), "plain", "utf-8")
    msg["Subject"] = Header(EMAIL_SUBJECT, "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = to_addr
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [to_addr], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"  ❌ 发送失败: {e}")
        return False

def save_csv_atomic(results):
    """原子化保存 CSV：先写临时文件，再替换原文件，避免写入中断损坏数据"""
    if not results:
        return
    results.sort(key=lambda x: int(x["appid"]))
    temp_file = OUTPUT_CSV + ".tmp"
    try:
        with open(temp_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["appid", "name", "email", "is_free", "is_delisted", "is_owned"])
            writer.writeheader()
            writer.writerows(results)
        os.replace(temp_file, OUTPUT_CSV)   # 原子替换
        print(f"\n💾 已保存 {len(results)} 条记录到 {OUTPUT_CSV}")
    except Exception as e:
        print(f"\n❌ 保存 CSV 失败: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

# ==================== 核心抓取流程（可复用） ====================
def scrape_batch(game_map, existing_results, start_label="", auto_mode=False):
    """
    抓取 game_map 中所有游戏，合并 existing_results，定期保存到 CSV。
    game_map: {appid: name} （已按升序排序）
    existing_results: 已有的其他结果（不会被覆盖）
    start_label: 可选的进度前缀
    auto_mode: 是否为 GitHub Actions 自动模式，是则开启超时控制
    """
    global _current_results, _current_auto_mode

    results = list(existing_results)
    sorted_items = sorted(game_map.items(), key=lambda x: int(x[0]))
    total = len(sorted_items)
    if total == 0:
        print("没有待处理的游戏。")
        return results

    delisted_set = load_delisted_apps()
    owned_set = load_owned_apps()

    print(f"{start_label} 共 {total} 个游戏待处理")
    if DELAY_BETWEEN_APPIDS > 0:
        print(f"预计剩余时间: {total * DELAY_BETWEEN_APPIDS / 60:.1f} 分钟")
    else:
        print("全速抓取模式")
    if auto_mode:
        print(f"自动模式：最大运行时间 {MAX_RUNTIME/3600:.1f} 小时")
    print("按 Ctrl+C 可安全中断")

    start_time = time.time()
    last_save_count = len(existing_results)
    try:
        for idx, (appid, name) in enumerate(sorted_items, 1):
            # 超时检查（自动模式）
            if auto_mode and (time.time() - start_time > MAX_RUNTIME):
                print(f"\n⏰ 达到最大运行时间，自动保存并退出。已处理 {idx-1}/{total} 个游戏。")
                break

            print(f"[{idx}/{total}] AppID: {appid}  {name[:40] if name else ''} ...", end=" ")

            # 邮箱
            email = get_support_email(appid)

            # 免费状态
            is_free, _ = get_game_details(appid)
            is_free_str = "True" if is_free else "False"

            # 下架状态
            is_delisted = appid in delisted_set
            is_delisted_str = "True" if is_delisted else "False"

            # 拥有状态
            is_owned = appid in owned_set
            is_owned_str = "True" if is_owned else "False"

            print(f"邮箱:{email} 免费:{is_free_str} 下架:{is_delisted_str} 拥有:{is_owned_str}")

            results.append({
                "appid": appid,
                "name": name,
                "email": email,
                "is_free": is_free_str,
                "is_delisted": is_delisted_str,
                "is_owned": is_owned_str
            })

            # 定期保存（每处理 SAVE_INTERVAL 个游戏后自动保存）
            if len(results) - last_save_count >= SAVE_INTERVAL:
                _current_results = results   # 让信号处理函数也能访问
                save_csv_atomic(results)
                last_save_count = len(results)

            if DELAY_BETWEEN_APPIDS > 0 and idx < total:
                time.sleep(DELAY_BETWEEN_APPIDS)

    except KeyboardInterrupt:
        print("\n⚠️ 用户中断，保存已抓取数据...")

    # 最终保存
    _current_results = results
    save_csv_atomic(results)
    return results

# ========================= 菜单功能 =========================
def option1_generate_csv(auto_mode=False):
    """选项一：完整续传（从最早未处理的开始）"""
    games = load_games_from_github()
    if not games:
        return

    processed_ids = set()
    existing_results = []
    if os.path.exists(OUTPUT_CSV):
        print(f"发现已有文件 {OUTPUT_CSV}，加载已处理 AppID...")
        try:
            with open(OUTPUT_CSV, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing_results.append(row)
                    processed_ids.add(row["appid"])
            print(f"  已加载 {len(processed_ids)} 条记录")
        except Exception as e:
            print(f"  读取失败: {e}，将从头开始")
            existing_results = []

    remaining = {aid: name for aid, name in games.items() if aid not in processed_ids}
    if not remaining:
        print("所有游戏都已处理完毕！")
        return

    scrape_batch(remaining, existing_results, auto_mode=auto_mode)

def option2_send_emails():
    """选项二：发送邮件"""
    if not os.path.exists(OUTPUT_CSV):
        print(f"❌ {OUTPUT_CSV} 不存在")
        return
    with open(OUTPUT_CSV, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    valid = [r for r in rows if r.get("email", "").strip().lower() not in ("nan", "failed", "")]
    if not valid:
        print("没有有效邮箱")
        return
    print(f"找到 {len(valid)} 个有效邮箱（共 {len(rows)} 条）")
    confirm = input("确认发送？(yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        return
    success = 0
    for idx, row in enumerate(valid, 1):
        print(f"[{idx}/{len(valid)}] 发送给 {row['name']} ({row['appid']}) → {row['email']}")
        if send_email(row["email"], row["name"]):
            success += 1
        if idx < len(valid):
            time.sleep(DELAY_BETWEEN_EMAILS)
    print(f"发送完毕。成功 {success}，失败 {len(valid)-success}")

def option3_retry_failed(auto_mode=False):
    """选项三：重试网络失败的邮箱（只重试 Failed）"""
    if not os.path.exists(OUTPUT_CSV):
        print(f"❌ {OUTPUT_CSV} 不存在")
        return

    with open(OUTPUT_CSV, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    failed_rows = [r for r in rows if r.get("email", "").strip().lower() == "failed"]
    if not failed_rows:
        print("✅ 没有 Failed 记录，无需重试。")
        return

    existing_results = [r for r in rows if r.get("email", "").strip().lower() != "failed"]
    games = load_games_from_github()
    failed_map = {r["appid"]: games.get(r["appid"], r.get("name", "")) for r in failed_rows}

    print(f"发现 {len(failed_rows)} 个 Failed，开始重新抓取...")
    scrape_batch(failed_map, existing_results, "重试 Failed", auto_mode=auto_mode)

def option4_start_from_appid(auto_mode=False):
    """选项四：从指定 AppID 开始续抓"""
    games = load_games_from_github()
    if not games:
        return

    start_appid = input("请输入起始 AppID (包含): ").strip()
    if not start_appid.isdigit():
        print("无效的 AppID，必须是数字。")
        return
    start_appid = str(int(start_appid))

    processed_ids = set()
    existing_results = []
    if os.path.exists(OUTPUT_CSV):
        print(f"发现已有文件 {OUTPUT_CSV}，加载已处理记录...")
        try:
            with open(OUTPUT_CSV, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing_results.append(row)
                    processed_ids.add(row["appid"])
            print(f"  已加载 {len(processed_ids)} 条记录")
        except Exception as e:
            print(f"  读取失败: {e}，将从头开始")
            existing_results = []

    remaining = {}
    skipped_count = 0
    for aid, name in games.items():
        if aid in processed_ids:
            continue
        if int(aid) < int(start_appid):
            skipped_count += 1
            continue
        remaining[aid] = name

    if not remaining:
        print(f"没有符合条件的游戏（已处理 {len(processed_ids)}，跳过了 {skipped_count} 个小于 {start_appid} 的未处理游戏）。")
        return

    print(f"已跳过 {skipped_count} 个小于 {start_appid} 的未处理游戏，剩余 {len(remaining)} 个")
    scrape_batch(remaining, existing_results, "指定起点", auto_mode=auto_mode)

def option5_supplement_status():
    """选项五：补充游戏状态列（免费/下架/拥有），不重新抓取邮箱"""
    if not os.path.exists(OUTPUT_CSV):
        print(f"❌ {OUTPUT_CSV} 不存在")
        return

    with open(OUTPUT_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    has_status_cols = all(c in fieldnames for c in ("is_free", "is_delisted", "is_owned"))
    needs_update = []
    for row in rows:
        if not has_status_cols:
            needs_update.append(row)
        else:
            is_free_val = row.get("is_free", "").strip()
            if not is_free_val:
                needs_update.append(row)

    if not needs_update:
        print("✅ 所有游戏的状态列都已完整，无需补充。")
        return

    print(f"发现 {len(needs_update)} 个游戏需要补充状态（共 {len(rows)} 行）。")
    confirm = input("确认开始补充？(yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        return

    delisted_set = load_delisted_apps()
    owned_set = load_owned_apps()
    row_dict = {r["appid"]: r for r in rows}

    total = len(needs_update)
    print(f"开始补充状态，延迟 {STATUS_DELAY}s（按 Ctrl+C 可安全中断）")
    try:
        for idx, row in enumerate(needs_update, 1):
            appid = row["appid"]
            name = row.get("name", "")
            print(f"[{idx}/{total}] AppID: {appid}  {name[:40] if name else ''} ...", end=" ")

            is_free, success = get_game_details(appid)
            if is_free is None and not success:
                is_free_str = "Unknown"
            else:
                is_free_str = "True" if is_free else "False"

            is_del = "True" if appid in delisted_set else "False"
            is_own = "True" if appid in owned_set else "False"

            print(f"免费:{is_free_str} 下架:{is_del} 拥有:{is_own}")
            row_dict[appid]["is_free"] = is_free_str
            row_dict[appid]["is_delisted"] = is_del
            row_dict[appid]["is_owned"] = is_own

            if STATUS_DELAY > 0 and idx < total:
                time.sleep(STATUS_DELAY)
    except KeyboardInterrupt:
        print("\n⚠️ 用户中断，保存已更新的数据...")

    updated_rows = sorted(row_dict.values(), key=lambda x: int(x["appid"]))
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["appid", "name", "email", "is_free", "is_delisted", "is_owned"])
        writer.writeheader()
        writer.writerows(updated_rows)

    still_missing = sum(1 for r in updated_rows if not r.get("is_free", "").strip())
    print(f"\n✅ 状态补充完成。仍有 {still_missing} 行的 is_free 为空（可能因网络错误）。")

def menu():
    """本地交互菜单"""
    while True:
        print("\n" + "="*60)
        print(" Steam Curator 工具（完整增强版）")
        print("="*60)
        print("1. 生成/续传表格（完整：邮箱+状态）")
        print("2. 发送合作邮件")
        print("3. 重试网络失败的邮箱 (Failed)")
        print("4. 从指定 AppID 开始续抓")
        print("5. 补充游戏状态（免费/下架/拥有）")
        print("0. 退出")
        choice = input("请输入选项: ").strip()
        if choice == "1":
            option1_generate_csv()
        elif choice == "2":
            option2_send_emails()
        elif choice == "3":
            option3_retry_failed()
        elif choice == "4":
            option4_start_from_appid()
        elif choice == "5":
            option5_supplement_status()
        elif choice == "0":
            print("再见！")
            sys.exit(0)
        else:
            print("无效选项")

# ========================= 主入口 =========================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Steam Curator 自动化工具")
    parser.add_argument("--mode", choices=["1","3","5"], help="非交互模式：1=续抓邮箱，3=重试Failed，5=补充状态")
    args = parser.parse_args()

    if args.mode:
        # GitHub Actions 自动模式，开启超时控制
        print("==== GitHub Actions 自动模式 ====")
        if args.mode == "1":
            option1_generate_csv(auto_mode=True)
        elif args.mode == "3":
            option3_retry_failed(auto_mode=True)
        elif args.mode == "5":
            option5_supplement_status()
    else:
        # 本地交互模式
        menu()