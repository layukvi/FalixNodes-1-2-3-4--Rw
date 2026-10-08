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
    email_selectors = [
        "input[type='email']", 
        "input[name='email']", 
        "#email-address", 
        "input[placeholder*='email' i]"
    ]
    pwd_selectors = [
        "input[type='password']", 
        "input[name='password']", 
        "#password", 
        "input[placeholder*='password' i]"
    ]

    found_email_sel = None
    for sel in email_selectors:
        if sb.is_element_visible(sel):
            found_email_sel = sel
            break

    if not found_email_sel:
        try:
            sb.wait_for_element_visible("input[type='email'], input[name='email'], #email-address", timeout=10)
            for sel in email_selectors:
                if sb.is_element_visible(sel):
                    found_email_sel = sel
                    break
        except Exception:
            pass

    if not found_email_sel:
        raise Exception("超时未找到邮箱输入框，可能页面未加载或被 Cloudflare 拦截")

    found_pwd_sel = None
    for sel in pwd_selectors:
        if sb.is_element_visible(sel):
            found_pwd_sel = sel
            break

    if not found_pwd_sel:
        raise Exception("找到邮箱输入框，但未找到密码输入框")

    try:
        sb.execute_script(f"""
            let el = document.querySelector("{found_email_sel}");
            el.value = '';
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        """)
        sb.type(found_email_sel, email)

        sb.execute_script(f"""
            let el = document.querySelector("{found_pwd_sel}");
            el.value = '';
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        """)
        sb.type(found_pwd_sel, password)

        print(f"[INFO] 成功填写表单 (使用了选择器: {found_email_sel})")
        return True
    except Exception as e:
        raise Exception(f"表单注入失败: {e}")


def robust_submit_form(sb) -> bool:
    submit_selectors = [
        "button[type='submit']",
        "button[name='submit']",
        "button:contains('Login')",
        "button:contains('Sign In')"
    ]
    try:
        sb.press_keys("input[type='password']", "\n")
        print("[INFO] 尝试使用回车键提交...")
        time.sleep(2)
        if "/auth/login" not in safe_get_url(sb):
            return True

        for sel in submit_selectors:
            if sb.is_element_visible(sel):
                sb.click(sel)
                print(f"[INFO] 点击了提交按钮 ({sel})")
                return True

        sb.execute_script("document.querySelector('button[type=\"submit\"]').click();")
        print("[INFO] 使用 JS 点击了提交按钮")
        return True
    except Exception as e:
        print(f"[WARN] 提交表单时发生异常: {e}")
        return False


# ---------- 广告弹窗与控制台状态 ----------
def handle_ad_modal(sb, server_id: str) -> bool:
    try:
        if sb.is_element_visible("#adModal"):
            print("[WARN] 检测到广告弹窗，刷新页面")
            shot(sb, f"ad-{server_id[:8]}")
            sb.refresh()
            time.sleep(3)
            return True
    except Exception:
        pass
    return False


def get_console_status(sb) -> str:
    try:
        elem = sb.find_element("#csb-status-text", timeout=5)
        return elem.text.strip().lower()
    except Exception:
        return "unknown"


def is_offline(status: str) -> bool:
    s = status.lower()
    return "offline" in s or "unknown" in s or s == ""


# ---------- 页面服务器列表解析 ----------
def fetch_servers_from_page(sb, email: str) -> Tuple[List[Dict], str]:
    email_safe = email_to_filename(email)

    cur_url = safe_get_url(sb)
    if BASE_URL not in cur_url or "/auth/" in cur_url:
        sb.open(BASE_URL)
        time.sleep(3)

    handle_cookie_consent(sb)
    last_shot = shot(sb, f"homepage-{email_safe}")

    target_selectors = [
        "a[href*='/server/']",
        "a.server-row-link",
        ".servers-container",
        "[class*='server-card']",
        "[class*='server-item']"
    ]

    found_selector = None
    print("[INFO] 正在寻找服务器列表...")

    start_time = time.time()
    while time.time() - start_time < 15:
        for sel in target_selectors:
            if sb.is_element_visible(sel):
                found_selector = sel
                break
        if found_selector:
            break
        time.sleep(1)

    if not found_selector:
        print("[ERROR] 服务器列表加载超时，未匹配到任何服务器卡片")
        last_shot = shot(sb, f"no-servers-{email_safe}")
        return [], last_shot

    print(f"[INFO] 成功匹配到服务器元素 (使用选择器: {found_selector})")
    last_shot = shot(sb, f"servers-loaded-{email_safe}")

    servers = []
    try:
        rows = sb.find_elements("a[href*='/server/']")
        print(f"[INFO] 发现 {len(rows)} 个服务器链接")

        seen_ids = set()
        for idx, row in enumerate(rows):
            try:
                href = row.get_attribute("href") or ""
                if "/server/" not in href:
                    continue

                parts = href.split("/server/")[1].split("/")
                server_id = parts[0]

                if not server_id or server_id in seen_ids:
                    continue
                seen_ids.add(server_id)

                name = f"Server-{server_id[:4]}"
                text_content = row.text.strip()
                if text_content:
                    first_line = [line.strip() for line in text_content.splitlines() if line.strip()]
                    if first_line:
                        name = first_line[0]

                print(f"[INFO]  [{len(servers)+1}] 名称: {name} (ID: {server_id[:8]}...)")
                servers.append({"id": server_id, "name": name})
            except Exception as e:
                print(f"[WARN] 解析第 {idx+1} 个服务器元素失败: {e}")

    except Exception as e:
        print(f"[ERROR] 查找服务器链接失败: {e}")

    last_shot = shot(sb, f"parsed-{len(servers)}svr-{email_safe}")
    print(f"[INFO] 共成功解析出 {len(servers)} 个服务器")
    return servers, last_shot


# ---------- 单个服务器重启逻辑 ----------
def check_and_restart_server(
    sb, server_id: str, server_name: str
) -> Tuple[bool, str, str]:
    console_url = f"{BASE_URL}/server/{server_id}/console"
    sid_short   = server_id[:8]
    last_shot   = ""

    for attempt in range(AD_RETRY_LIMIT):
        sb.open(console_url)
        time.sleep(random.uniform(2, 5))

        for _ in range(3):
            if not sb.is_element_visible("#accept-choices"):
                break
            handle_cookie_consent(sb)
            time.sleep(1)

        last_shot = shot(sb, f"console-{sid_short}-a{attempt+1}")
        status = get_console_status(sb)
        print(f"[INFO] {server_name}  状态=[{status}]  尝试={attempt+1}")

        if not is_offline(status):
            last_shot = shot(sb, f"online-{sid_short}")
            return True, f"在线 ({status})", last_shot

        try:
            sb.click("#startbutton", timeout=5)
            print(f"[INFO] 已点击 Start（{attempt+1}/{AD_RETRY_LIMIT}）")
            time.sleep(6)
            last_shot = shot(sb, f"after-start-{sid_short}-a{attempt+1}")
        except Exception as e:
            print(f"[WARN] 点击 Start 失败: {e}")

        if handle_ad_modal(sb, server_id):
            continue

        new_status = get_console_status(sb)
        print(f"[INFO] {server_name}  启动后状态=[{new_status}]")
        if not is_offline(new_status):
            last_shot = shot(sb, f"restarted-{sid_short}")
            return True, f"重启成功 ({new_status})", last_shot

        time.sleep(3)

    last_shot = shot(sb, f"fail-{sid_short}")
    return False, "重启失败（超出重试次数）", last_shot


# ---------- 主控逻辑 ----------
def login_and_restart(email: str, password: str, proxy: Optional[str]) -> Dict:
    result = {
        "success": False,
        "email":   email,
        "servers_checked":   0,
        "servers_restarted": 0,
        "message":        "",
        "server_details": [],
        "screenshots":    [],
    }
    email_log  = mask_email(email)
    email_safe = email_to_filename(email)

    print("\n" + "=" * 60)
    print(f"[INFO] 账号: {email_log}")
    print("=" * 60)

    with SB(uc=True, test=True, locale="en",
            proxy=proxy, headed=not is_linux()) as sb:

        logged_in = False

        for attempt in range(MAX_RETRY):
            print(f"\n[INFO] 登录尝试 {attempt+1}/{MAX_RETRY}")

            sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10.0)
            time.sleep(3)

            src_lower = safe_get_source(sb).lower()
            if "access denied" in src_lower or "403 forbidden" in src_lower or "attention required" in src_lower:
                print("[ERROR] IP 被 Cloudflare 拦截拒绝访问 (Access Denied / 403)。")
                shot(sb, f"ip-blocked-{email_safe}")
                result["message"] = "IP被墙"
                return result

            shot(sb, f"login-open-{email_safe}-a{attempt+1}")
            cur = safe_get_url(sb)
            if "/auth/login" not in cur:
                print("[INFO] Session 有效，已自动跳转")
                logged_in = True
                break

            print("[INFO] 处理登录盾（Turnstile）...")
            turnstile_ok = wait_turnstile(sb, timeout=90)
            shot(sb, f"after-turnstile-{email_safe}-a{attempt+1}")

            if not turnstile_ok:
                print("[WARN] Turnstile 未完成，重试本次登录")
                continue

            try:
                robust_fill_form(sb, email, password)
            except Exception as e:
                print(f"[ERROR] {e}")
                shot(sb, f"form-error-{email_safe}-a{attempt+1}")
                continue

            if not _turnstile_token_ready(sb):
                print("[INFO] 填表后 CF Token 可能刷新，等待缓冲...")
                time.sleep(2)

            shot(sb, f"before-submit-{email_safe}-a{attempt+1}")

            if robust_submit_form(sb):
                print("[INFO] 表单已提交请求")

            time.sleep(6)
            shot(sb, f"after-submit-{email_safe}-a{attempt+1}")

            cur = safe_get_url(sb)
            if "/auth/login" not in cur:
                logged_in = True
                print(f"[INFO] ✅ 登录成功 → {cur}")
                break

            src = safe_get_source(sb).lower()
            if any(kw in src for kw in ("invalid", "incorrect", "failed", "wrong credential")):
                print("[ERROR] 检测到账号或密码错误提示，停止重试")
                break

            print(f"[WARN] 仍在登录页，将重试")

        if not logged_in:
            result["message"] = "登录失败，可能是UI变更或网络问题"
            result["screenshots"] = [shot(sb, f"login-fail-{email_safe}")]
            return result

        try:
            sb.execute_script("""
                setInterval(function() {
                    var btn = document.getElementById('accept-choices');
                    if (btn) {
                        var container = btn.closest('.sn-inner') || btn.parentElement;
                        if (container) container.remove();
                    }
                }, 1000);
            """)
        except Exception:
            pass

        servers, list_shot = fetch_servers_from_page(sb, email)
        result["screenshots"].append(list_shot)
        if not servers:
            result["message"] = "未找到服务器"
            return result

        result["servers_checked"] = len(servers)
        restarted = 0
        for idx, svr in enumerate(servers, 1):
            print(f"\n[INFO] ── 检查 {idx}/{len(servers)}: {svr['name']} ──")
            ok, desc, svr_shot = check_and_restart_server(sb, svr["id"], svr["name"])
            result["server_details"].append({
                "id":     svr["id"],
                "name":   svr["name"],
                "status": desc,
            })
            result["screenshots"].append(svr_shot)
            if "重启成功" in desc:
                restarted += 1

        result["success"] = True
        result["servers_restarted"] = restarted
        result["message"] = f"检查 {len(servers)} 台，重启 {restarted} 台"
        return result


# ---------- 启动函数 ----------
def main():
    proxy   = os.environ.get("PROXY_SERVER")
    display = None

    if is_linux() and not os.environ.get("DISPLAY"):
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
            print("[INFO] 虚拟显示已启动")
        except Exception as e:
            print(f"[ERROR] 虚拟显示失败: {e}")
            sys.exit(1)

    accounts = parse_accounts()
    if not accounts:
        sys.exit("[ERROR] 未找到账号配置（环境变量 FALIX）")

    print(f"[INFO] 共 {len(accounts)} 个账号")

    results = []
    for idx, acc in enumerate(accounts, 1):
        print(f"\n{'#'*60}\n# 账号 {idx}/{len(accounts)}\n{'#'*60}")
        res = login_and_restart(acc["email"], acc["password"], proxy)
        results.append(res)

        notify(
            ok=res["success"],
            email=res["email"],
            summary=res["message"],
            server_details=res.get("server_details", []),
            screenshots=res.get("screenshots", []),
        )

        if idx < len(accounts):
            delay = random.randint(10, 25)
            print(f"[INFO] 等待 {delay}s 后处理下一账号...")
            time.sleep(delay)

    ok_cnt  = sum(1 for r in results if r["success"])
    checked = sum(r.get("servers_checked", 0)   for r in results)
    restart = sum(r.get("servers_restarted", 0) for r in results)

    print("\n" + "=" * 60)
    print(f"[INFO] 完成！成功账号: {ok_cnt}/{len(results)}")
    print(f"[INFO] 检查服务器: {checked}  重启: {restart}")
    print(f"[INFO] 截图总数: {screenshot_counter['count']}")
    print("=" * 60)

    if display:
        display.stop()

    sys.exit(0 if ok_cnt == len(results) else 1)


if __name__ == "__main__":
    main()
