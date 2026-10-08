# ---------- 从页面解析服务器列表 (优化版) ----------
def fetch_servers_from_page(sb, email: str) -> Tuple[List[Dict], str]:
    email_safe = email_to_filename(email)
    
    # 1. 检查当前是否已经在主页，不在才需要 open，避免二次刷新触发拦截
    cur_url = safe_get_url(sb)
    if BASE_URL not in cur_url or "/auth/" in cur_url:
        sb.open(BASE_URL)
        time.sleep(3)
        
    handle_cookie_consent(sb)
    last_shot = shot(sb, f"homepage-{email_safe}")

    # 2. 通用选择器列表（匹配 FalixNodes 各种新旧改版UI）
    target_selectors = [
        "a[href*='/server/']",
        "a.server-row-link",
        ".servers-container",
        "[class*='server-card']",
        "[class*='server-item']"
    ]

    found_selector = None
    print("[INFO] 正在寻找服务器列表...")
    
    # 循环等待，最高等待 15 秒
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

    # 3. 提取所有匹配到的服务器链接
    servers = []
    try:
        # 获取所有包含 /server/ 的 <a> 链接标签
        rows = sb.find_elements("a[href*='/server/']")
        print(f"[INFO] 发现 {len(rows)} 个服务器链接")
        
        seen_ids = set()
        for idx, row in enumerate(rows):
            try:
                href = row.get_attribute("href") or ""
                if "/server/" not in href:
                    continue
                
                # 提取 server_id
                parts = href.split("/server/")[1].split("/")
                server_id = parts[0]
                
                # 过滤重复的服务器 ID
                if not server_id or server_id in seen_ids:
                    continue
                seen_ids.add(server_id)

                # 获取服务器名称
                name = f"Server-{server_id[:4]}"
                text_content = row.text.strip()
                if text_content:
                    # 取第一行非空文本作为名称
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
