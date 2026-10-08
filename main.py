#!/usr/bin/env python3

import os
import sys
import time
import json
import platform
import random
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone, timedelta

import requests
from seleniumbase import SB

# ---------- 配置 ----------
BASE_URL   = "https://client.falixnodes.net"
LOGIN_URL  = f"{BASE_URL}/auth/login"
OUTPUT_DIR = Path("output/falix")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_RETRY      = 3
AD_RETRY_LIMIT = 10  # Start 重试次数
CN_TZ = timezone(timedelta(hours=8))

screenshot_counter = {"count": 0}


# ---------- 工具函数 ----------
def cn_time() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def is_linux() -> bool:
    return platform.system().lower() == "linux"


def email_to_filename(email: str) -> str:
    if not email or "@" not in email:
        return "unknown"
    local, domain = email.split("@", 1)
    domain_short = domain.replace(".", "")[-4:] if domain else "xx"
    return f"{local[0]}_{domain_short}"


def shot(sb, name: str) -> str:
    screenshot_counter["count"] += 1
    ts   = datetime.now(CN_TZ).strftime("%H%M%S")
    safe = re.sub(r'[":><|*?\r\n/\\]', "", name)
    fp   = str(OUTPUT_DIR / f"{screenshot_counter['count']:03d}-{ts}-{safe}.png")
    try:
        sb.save_screenshot(fp)
    except Exception as e:
        print(f"[ERROR] 截图失败: {e}")
    return fp


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[0]}***@***{domain[-2:]}"


def safe_get_url(sb) -> str:
    try:
        return sb.get_current_url()
    except Exception:
        return ""


def safe_get_source(sb) -> str:
    try:
        return sb.get_page_source()
    except Exception:
        return ""


# ---------- Telegram 通知 ----------
def notify(
    ok: bool,
    email: str = "",
    summary: str = "",
    server_details: List[Dict] = None,
    screenshots: List[str] = None,
):
    token   = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return

    try:
        text = f"{'✅ 成功' if ok else '❌ 失败'}\n"
        text += f"账号: {email}\n"
        text += f"信息: {summary}\n"
        for d in (server_details or []):
            server_display = d.get('id') or d.get('name', '?')
            text += f"服务器: {server_display}  {d.get('status','?')}\n"
        text += f"时间: {cn_time()}\n\nFalixNodes Auto Restart"

        if screenshots:
            last_shot = screenshots[-1]
            if last_shot and Path(last_shot).exists():
                with open(last_shot, "rb") as f:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat_id, "caption": text},
                        files={"photo": f},
                        timeout=60,
                    )
            else:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=30,
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=30,
            )
    except Exception as e:
        print(f"[ERROR] TG 通知失败: {e}")


# ---------- 解析账号 ----------
def parse_accounts() -> List[Dict]:
    raw = os.environ.get("FALIX", "")
    accounts = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "-----" not in line:
            continue
        email, pwd = line.split("-----", 1)
        accounts.append({"email": email.strip(), "password": pwd.strip()})
    return accounts


# ---------- Cookie 弹窗处理 ----------
def handle_cookie_consent(sb) -> bool:
    selectors = [
        "#accept-choices",
        "div.sn-b-def.sn-blue",
        "button:contains('Accept')",
        "a:contains('Accept')",
    ]
    for sel in selectors:
        try:
            sb.wait_for_element_visible(sel, timeout=3)
            sb.click(sel)
            print(f"[INFO] Cookie 弹窗已关闭 (点击 {sel})")
            time.sleep(1)
            return True
        except Exception:
            continue

    try:
        sb.execute_script("""
            var el = document.querySelector('.sn-inner') || 
                     document.querySelector('.sn-b-def.sn-blue')?.closest('.sn-inner');
            if (el) el.remove();
            var overlays = document.querySelectorAll('[class*="sn-"], [id*="accept"]');
            for (var i=0; i<overlays.length; i++) {
                var style = window.getComputedStyle(overlays[i]);
                if (style.position === 'fixed' || style.position === 'absolute') {
                    if (overlays[i].offsetHeight > 100) overlays[i].remove();
                }
            }
            document.body.style.overflow = '';
            document.documentElement.style.overflow = '';
        """)
    except Exception:
        pass
    return False


# ---------- Turnstile 处理 ----------
def _turnstile_token_ready(sb) -> bool:
    try:
        token_ok = sb.execute_script("""
            var inp = document.querySelector("input[name='cf-turnstile-response']");
            return inp && inp.value && inp.value.length > 20;
        """)
        if token_ok:
            return True
    except Exception:
        pass
    return False


def _try_click_turnstile(sb) -> bool:
    try:
        sb.uc_gui_click_captcha()
        return True
    except Exception:
        pass

    try:
        sb.switch_to_frame("iframe[src*='challenges.cloudflare']")
        sb.click("input[type='checkbox'], .cb-lb", timeout=2)
        sb.switch_to_default_content()
        return True
    except Exception:
        try:
            sb.switch_to_default_content()
        except Exception:
            pass

    try:
        sb.execute_script("""
            var ts = document.querySelector('.cf-turnstile');
            if (ts) ts.click();
        """)
        return True
    except Exception:
        pass
    return False


def wait_turnstile(sb, timeout: int = 60) -> bool:
    try:
        has = sb.execute_script("""
            return !!(
                document.querySelector('.cf-turnstile') ||
                document.querySelector('iframe[src*="challenges.cloudflare"]') ||
                document.querySelector('input[name="cf-turnstile-response"]')
            );
        """)
    except Exception:
        has = False

    if not has:
        return True

    print("[INFO] 发现 Turnstile，开始等待验证完成...")
    start = time.time()
    last_click = 0

    while time.time() - start < timeout:
        if _turnstile_token_ready(sb):
            print("[INFO] ✅ Turnstile 验证完成")
            time.sleep(2)
            return True

        now = time.time()
        if now - last_click >= 4:
            _try_click_turnstile(sb)
            last_click = now

        time.sleep(1)

    if _turnstile_token_ready(sb):
        return True

    print("[WARN] ⚠️ Turnstile 等待超时")
    return False


# ---------- 登录表单处理 ----------
def robust_fill_form(sb, email: str, password: str) -> bool:
    email_selectors =
