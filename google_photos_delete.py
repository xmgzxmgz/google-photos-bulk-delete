r"""
Google 相簿批量删除脚本（本地运行版 · 真实 profile + CDP 连接）

原理：
- Playwright 用 launch_persistent_context 加载"默认 Chrome 用户目录"会被 Chrome 拒绝
  （"DevTools remote debugging requires a non-default data directory"）。
- 克隆 profile 到临时目录又会撞 Chrome 127+ 的应用绑定加密，cookie 解不开 -> 停在介绍页。
- 因此本脚本用"真实 Chrome 可执行文件 + 真实 User Data"启动一个带 --remote-debugging-port
  的 Chrome（登录态/加密密钥都对得上），再用 Playwright 的 connect_over_cdp 接管它。
- 启动时就直接把 photos.google.com 作为命令行参数传给 Chrome，避免"连接后挑错标签页"的问题。

核心思路（区别于多数开源工具的地方）：
- 利用 Google 相簿"日期分组头部复选框"（aria-label 形如"选择以下日期的所有照片：6月16日周二"），
  点 1 次 = 选中那一整天所有照片，远比逐张/逐屏勾选高效。
- 每次点击后校验选中计数是否真的增长，没涨就把该日期拉黑（避免"勾上又取消"来回白忙）。
- 删除确认对话框的遮罩会拦截对顶栏按钮的二次点击，因此确认按钮严格限定在 role=dialog 内 force 点击。

安全闸（默认全部偏向安全；必须通过环境变量或改代码显式放开才会真正删除）：
  GPBD_DRY_RUN=0      -> 关闭 dry-run，进入真实删除准备
  GPBD_BACKUP_DONE=1  -> 确认已去 takeout.google.com 备份相册
  GPBD_FULL_DELETE=1  -> 真正全量删除；之前必须先试删一小批确认无误
所有路径（Chrome 可执行文件、User Data 目录、profile 名）均可通过 GPBD_* 环境变量覆盖，
默认值按平台自动探测。

运行：
  1) pip install playwright && playwright install chromium
  2) 彻底关掉 Chrome（含后台）： taskkill /F /IM chrome.exe   (Windows)
  3) python google_photos_delete.py   # 默认 DRY_RUN，先验证能否进图库、能否选中照片
"""

import os
import re
import sys
import time
import json
import shutil
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ============ 配置（均可通过环境变量覆盖；默认值按平台自动探测） ============
def _default_user_data_dir():
    if sys.platform.startswith("win"):
        return os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    if sys.platform == "darwin":
        return str(Path.home() / "Library/Application Support/Google/Chrome")
    return str(Path.home() / ".config/google-chrome")


def detect_chrome_exe():
    """按平台自动探测 Chrome 可执行文件；找不到返回 None。"""
    env = os.environ.get("GPBD_CHROME_EXE")
    if env and os.path.exists(env):
        return env
    if sys.platform.startswith("win"):
        cands = [
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        cands = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                 "/usr/bin/chromium", "/usr/bin/chromium-browser"]
    for c in cands:
        if os.path.exists(c):
            return c
    try:
        which = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if which:
            return which
    except Exception:
        pass
    return None


# 真实 Chrome 用户数据目录（含登录态 / cookie 加密密钥）。可用 GPBD_CHROME_USER_DATA 覆盖。
CHROME_USER_DATA_DIR = os.environ.get("GPBD_CHROME_USER_DATA") or _default_user_data_dir()
# 多账号时改 Profile 1 / Profile 2 ...；可用 GPBD_CHROME_PROFILE 覆盖。
CHROME_PROFILE = os.environ.get("GPBD_CHROME_PROFILE", "Default")

# junction（目录联接）目录：Chrome 拒绝在默认用户目录上开远程调试端口，
# 因此用一个非默认路径名的 junction 指向同一份真实文件，绕过限制。
# 可用 GPBD_CHROME_LINK 覆盖（默认：脚本所在目录下的 chrome_link）。
CHROME_LINK_DIR = os.environ.get("GPBD_CHROME_LINK") or str(Path(__file__).resolve().parent / "chrome_link")

# Chrome 可执行文件路径；默认自动探测，也可用 GPBD_CHROME_EXE 指定（由 main() 启动时填充）。
CHROME_EXE = None
TARGET_URL = "https://photos.google.com"

# ---------- 安全闸（默认全部偏向安全；必须显式放开才会真正删除） ----------
# 亦可通过环境变量覆盖：GPBD_DRY_RUN / GPBD_BACKUP_DONE / GPBD_FULL_DELETE
DRY_RUN = (os.environ.get("GPBD_DRY_RUN", "1").lower() not in ("0", "false", "no"))
BACKUP_DONE = (os.environ.get("GPBD_BACKUP_DONE", "0").lower() in ("1", "true", "yes"))
FULL_DELETE = (os.environ.get("GPBD_FULL_DELETE", "0").lower() in ("1", "true", "yes"))
TEST_BATCH = int(os.environ.get("GPBD_TEST_BATCH", "10"))        # 试删阶段累计上限
BATCH_TARGET = int(os.environ.get("GPBD_BATCH_TARGET", "2000"))  # 每轮累积选中并一次删除的目标张数
BATCH_PAUSE = float(os.environ.get("GPBD_BATCH_PAUSE", "1.0"))   # 每批之间停顿（秒）
CLICK_PAUSE = float(os.environ.get("GPBD_CLICK_PAUSE", "0.08")) # 选中单张之间的停顿（秒）

# ---------- 代理（自动读系统设置，失败则回退到常见端口） ----------
PROXY = None            # 由 detect_proxy() 在运行时填充
CDP_PORT = 9222         # 运行时会自动找一个空闲端口


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _ws(name):
    return str(Path(__file__).resolve().parent / name)


def detect_proxy():
    """读取 Windows 系统代理设置；没有就回退常见 Clash 端口。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if enabled:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if server:
                for part in server.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k.strip().lower() in ("http", "https", "socks"):
                            return v.strip()
                    else:
                        return part.strip()
    except Exception:
        pass
    for cand in ("127.0.0.1:7897", "127.0.0.1:7890", "127.0.0.1:1080"):
        if _port_open(cand):
            return cand
    return None


def _port_open(addr):
    import socket
    host, port = addr.split(":")
    try:
        s = socket.create_connection((host, int(port)), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def _free_port():
    """找一个当前空闲的本地端口，避免被上次崩溃遗留的 Chrome 占用。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check_proxy(proxy):
    if not proxy:
        return True
    if _port_open(proxy):
        log(f"代理探活成功（{proxy} 可达），通道可用")
        return True
    log(f"代理端口不可达：{proxy}")
    log("   请确认 Clash 正在运行，或修改 detect_proxy() 的回退端口。")
    return False


def chrome_running():
    try:
        out = subprocess.run(
            ["tasklist"], capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=10,
        ).stdout.lower()
        return "chrome.exe" in out
    except Exception:
        return False


def clear_singleton_lock():
    """Chrome 完全关闭后，删除可能残留的所有 Singleton* 锁文件，避免"握手交给旧实例/锁冲突"。
    注意锁文件位于真实目录（junction 解析后），所以要清真实目录。
    """
    bases = [Path(CHROME_USER_DATA_DIR), Path(CHROME_USER_DATA_DIR) / CHROME_PROFILE]
    names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
    for base in bases:
        for n in names:
            p = base / n
            try:
                if p.exists():
                    p.unlink()
                    log(f"已清理残留锁文件：{p}")
            except Exception as e:
                log(f"（清理 {p} 失败，可忽略）：{e}")


def ensure_junction():
    """确保 junction 存在且指向真实目录。"""
    link = Path(CHROME_LINK_DIR)
    real = Path(CHROME_USER_DATA_DIR)
    if link.exists():
        try:
            if link.is_junction():
                return
        except Exception:
            pass
        if link.resolve() == real.resolve():
            return
        log(f"⚠️ {link} 已存在但不是 junction，请手动处理后重跑。")
        sys.exit("junction 路径冲突，已中止。")
    try:
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"New-Item -ItemType Junction -Path '{link}' -Target '{real}' | Out-Null",
            ],
            check=True, timeout=20,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log(f"已建立 junction：{link} -> {real}")
    except Exception as e:
        log(f"建立 junction 失败：{e}")
        log("   可手动在 PowerShell 执行：")
        log(f"   New-Item -ItemType Junction -Path '{link}' -Target '{real}'")
        sys.exit("junction 建立失败，已中止。")


def chrome_cdp_ready(port, timeout=40):
    """轮询 CDP 调试端口是否就绪，返回 /json/version 的解析结果或 None。"""
    for i in range(timeout):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            ) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                    log(f"Chrome 调试端口已就绪（{port}）：{data.get('Browser','')}")
                    return data
        except Exception:
            if i % 5 == 0:
                log(f"  等待调试端口就绪…（已 {i} 秒）")
            time.sleep(1)
    return None


def launch_chrome_cdp(proxy, port):
    """用真实 Chrome + 真实 profile 启动一个带 CDP 调试端口、并直接打开相簿的实例。"""
    args = [
        CHROME_EXE,
        TARGET_URL,                                   # 直接让 Chrome 打开相簿
        f"--user-data-dir={CHROME_LINK_DIR}",
        f"--profile-directory={CHROME_PROFILE}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "--disable-background-networking",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
        args.append("--proxy-bypass-list=<-loopback>")
    args = [a for a in args if a]
    log(f"启动 Chrome（真实 profile + CDP 端口 {port} + 直接打开 {TARGET_URL}）…")
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


def safe_goto(page, url, retries=3):
    last = None
    for i in range(retries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return True
        except Exception as e:
            last = e
            log(f"页面加载失败（第 {i+1}/{retries} 次）：{e}，3 秒后重试")
            time.sleep(3)
    raise last


def count_tiles(page):
    return page.evaluate(
        """() => {
            const cand = document.querySelectorAll(
                'grid-item, div[role="button"][data-testid], ' +
                'div[jsaction*="click"][aria-label], ' +
                'div[role="button"] img, a[href*="/photo/"]'
            );
            return cand.length;
        }"""
    )


def _visible_checkbox(page, aria_substr=None):
    """在 page 上找一个当前可见的角色为 checkbox 的元素；aria_substr 可限定 label。"""
    js = """(sub) => {
        const nodes = Array.from(document.querySelectorAll(
            '[role="checkbox"], button[aria-label*="选择"], button[aria-label*="Select"]'
        ));
        for (const n of nodes) {
            const r = n.getBoundingClientRect();
            const cs = getComputedStyle(n);
            if (r.width <= 0 || r.height <= 0) continue;
            if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
            if (!sub) return n;
            const lbl = (n.getAttribute('aria-label') || '') + ' ' + (n.textContent || '');
            if (lbl.includes(sub)) return n;
        }
        return null;
    }"""
    handle = page.evaluate_handle(js, aria_substr)
    el = handle.as_element()
    return el


def _safe_screenshot(page, name):
    """截图失败也不致命。"""
    try:
        page.screenshot(path=_ws(name), timeout=5000)
        log(f"  截图：{name}")
    except Exception as e:
        log(f"  截图失败（忽略）：{e}")


def _js_find_element(page, js_expr, arg=None):
    """执行 JS 表达式，返回一个 element handle（找不到返回 None）。"""
    handle = page.evaluate_handle(js_expr, arg)
    try:
        return handle.as_element()
    except Exception:
        return None


def bulk_select_visible(page, min_batch=50, max_tries=4):
    """
    进入选择态并尽量多选当前已加载的照片。
    流程：hover 第一张照片 → 点其圆形复选框 → 顶栏出现"已选择 N 项"返回 N。
    若首轮勾选数量 < min_batch，会再多滚屏多试几次（最多 max_tries 次）。
    任何一步失败返回 -1，绝不抛异常。
    """
    for attempt in range(max_tries):
        log(f"  bulk_select 尝试 {attempt+1}/{max_tries}")
        n = _bulk_select_one(page)
        if n and n >= min_batch:
            return n
        if n and n > 0:
            log(f"  本轮仅勾到 {n} 张，继续滚屏以触发更多自动勾选…")
        else:
            log(f"  本轮未成功勾选，继续滚屏…")
        scroll_to_load_more(page, max_scrolls=10)
        time.sleep(1.0)
    return _bulk_select_one(page)


def _bulk_select_one(page):
    """
    进入选择态并尽量多选当前已加载的照片。
    流程：鼠标移到第一张瓦片中心（触发 hover → 浮现圆形复选框）→ 点该复选框
        → 顶栏出现『全选』→ 点『全选』→ 读『已选择 N 张』返回 N。
    任何一步失败返回 -1，绝不抛异常。
    """
    try:
        n_tiles = _wait_tiles(page, timeout=90)
        if n_tiles <= 0:
            log("  等待 90s 仍未出现照片瓦片（页面可能仍在加载或图库已空）。")
            return -1
        first_tile = _js_find_element(page, r"""() => {
            const links = document.querySelectorAll('a[href*="/photo/"]');
            for (const a of links) {
                const r = a.getBoundingClientRect();
                if (r.width < 60 || r.height < 60) continue;
                const cs = getComputedStyle(a);
                if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                if (r.top < 0 || r.left < 0) continue;
                if (r.top > window.innerHeight || r.left > window.innerWidth) continue;
                return a;
            }
            return null;
        }""")
        if first_tile is None:
            log("  未找到可见的照片瓦片。")
            return -1
        box = first_tile.bounding_box()
        if not box:
            return -1
        try:
            first_tile.hover()
        except Exception:
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        time.sleep(0.6)

        cb = _js_find_element(page, r"""() => {
            const links = document.querySelectorAll('a[href*="/photo/"]');
            let tile = null;
            for (const a of links) {
                const r = a.getBoundingClientRect();
                if (r.width >= 60 && r.height >= 60 && r.top >= 0 && r.left >= 0) {
                    tile = a; break;
                }
            }
            const tb = tile ? tile.getBoundingClientRect() : null;
            const cbs = Array.from(document.querySelectorAll(
                '[role="checkbox"], button[aria-label*="选择"], button[aria-label*="Select"]'
            ));
            if (tb) {
                for (const c of cbs) {
                    const r = c.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const cs = getComputedStyle(c);
                    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                    if (r.left >= tb.left - 8 && r.right <= tb.left + tb.width / 2 &&
                        r.top >= tb.top - 8 && r.bottom <= tb.top + tb.height / 2) {
                        return c;
                    }
                }
            }
            for (const c of cbs) {
                const r = c.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                const cs = getComputedStyle(c);
                if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                return c;
            }
            return null;
        }""")
        if cb is None:
            log("  hover 后未在第一张瓦片内找到可见的复选框。")
            return -1
        try:
            cb.click(force=True, timeout=5000)
        except Exception:
            try:
                page.evaluate("(e) => e.click()", cb)
            except Exception:
                return -1

        # Google 相簿没有『全选』复选框（已实测确认），这里只负责进入选择态。
        for _ in range(12):
            time.sleep(0.4)
            c = _read_selected_count(page)
            if c and c > 0:
                return c
        return -1
    except Exception as e:
        log(f"  进入选择态出错（已吞掉）：{e}")
        return -1


def _read_selected_count(page):
    """读顶栏『已选择 N 项/张』，返回整数或 None。"""
    try:
        return page.evaluate(
            r"""() => {
                const els = Array.from(document.querySelectorAll('*'));
                for (const e of els) {
                    const t = (e.textContent || '').trim();
                    const m = t.match(/已选择\s*([\d,]+)\s*(?:项|张)/);
                    if (m) return parseInt(m[1].replace(/,/g, ''), 10);
                }
                return null;
            }"""
        )
    except Exception:
        return None


def _find_trash_button(page):
    """在顶栏内按关键词找删除（废纸篓）图标按钮。返回 element handle 或 None。"""
    try:
        handle = page.evaluate_handle(
            r"""() => {
                const kw = ['trash','delete','废纸篓','删除','移除','move to trash','回收站'];
                const kwLow = kw.map(s => s.toLowerCase());
                const sels = ['button', '[role="button"]', 'a', 'div'];
                for (const s of sels) {
                    for (const e of document.querySelectorAll(s)) {
                        const r = e.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const cs = getComputedStyle(e);
                        if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                        if (r.top > 120) continue;
                        const label = ((e.getAttribute('aria-label') || '') + ' '
                            + (e.getAttribute('title') || '') + ' ' + (e.textContent || '')).toLowerCase();
                        for (const k of kwLow) {
                            if (label.includes(k)) return e;
                        }
                    }
                }
                return null;
            }"""
        )
        return handle.as_element()
    except Exception:
        return None


def _dialog_open(page):
    """确认对话框（role=dialog）是否打开。"""
    try:
        return bool(page.evaluate(
            r"""() => !!document.querySelector('div[role="dialog"]')"""
        ))
    except Exception:
        return False


def _click_toolbar_trash(page):
    """在顶栏内按关键词找并强制点击『移至回收站』按钮，打开确认对话框。"""
    try:
        handle = page.evaluate_handle(
            r"""() => {
                const kw = ['移至回收站','移动到回收站','回收站','删除','移除','move to trash','trash','delete'];
                const kwLow = kw.map(s => s.toLowerCase());
                const sels = ['button', '[role="button"]'];
                for (const s of sels) {
                    for (const e of document.querySelectorAll(s)) {
                        const r = e.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const cs = getComputedStyle(e);
                        if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                        if (r.top > 120) continue;
                        const label = ((e.getAttribute('aria-label') || '') + ' '
                            + (e.getAttribute('title') || '') + ' ' + (e.textContent || '')).toLowerCase();
                        for (const k of kwLow) {
                            if (label.includes(k)) return e;
                        }
                    }
                }
                return null;
            }"""
        )
        el = handle.as_element()
        if el is None:
            return False
        try:
            el.click(force=True, timeout=5000)
            return True
        except Exception:
            try:
                page.evaluate("(e) => e.click()", el)
                return True
            except Exception:
                return False
    except Exception:
        return False


def _confirm_dialog(page):
    """在确认对话框内点击确认按钮（范围限定在 role=dialog 内，排除『取消』）。
    成功返回 True；找不到/点击失败返回 False。"""
    try:
        page.wait_for_selector('div[role="dialog"]', timeout=8000)
    except Exception:
        pass
    try:
        handle = page.evaluate_handle(
            r"""() => {
                const dlg = document.querySelector('div[role="dialog"]');
                if (!dlg) return null;
                const kw = ['移至回收站','移动到回收站','回收站','删除','移除','确认','move to trash','delete','remove'];
                const kwLow = kw.map(s => s.toLowerCase());
                const sels = ['button', '[role="button"]'];
                let best = null, bestScore = -1e9;
                for (const s of sels) {
                    for (const e of dlg.querySelectorAll(s)) {
                        const r = e.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const cs = getComputedStyle(e);
                        if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                        const label = ((e.getAttribute('aria-label') || '') + ' '
                            + (e.getAttribute('title') || '') + ' ' + (e.textContent || '')).toLowerCase();
                        if (label.includes('取消') || label.includes('cancel')) continue;
                        let matched = false;
                        for (const k of kwLow) {
                            if (label.includes(k)) { matched = true; break; }
                        }
                        if (!matched) continue;
                        const score = r.top * 10000 + r.left;
                        if (score > bestScore) { bestScore = score; best = e; }
                    }
                }
                return best;
            }"""
        )
        el = handle.as_element()
        if el is None:
            try:
                handle2 = page.evaluate_handle(
                    r"""() => {
                        const dlg = document.querySelector('div[role="dialog"]');
                        if (!dlg) return null;
                        const b = dlg.querySelectorAll('button, [role="button"]');
                        return b.length ? b[b.length - 1] : null;
                    }"""
                )
                el = handle2.as_element() if handle2 else None
            except Exception:
                el = None
        if el is None:
            return False
        try:
            el.click(force=True, timeout=5000)
            time.sleep(2.0)
            return True
        except Exception:
            try:
                page.evaluate("(e) => e.click()", el)
                time.sleep(2.0)
                return True
            except Exception:
                return False
    except Exception:
        return False


def _has_trash_snackbar(page):
    """检测是否出现『已移到废纸篓 / Moved to trash』提示条。"""
    try:
        return page.evaluate(
            r"""() => {
                const els = Array.from(document.querySelectorAll('*'));
                for (const e of els) {
                    const t = (e.textContent || '').toLowerCase();
                    if (t.includes('moved to trash') || t.includes('已移到废纸篓')
                        || t.includes('移到废纸篓')) return true;
                }
                return false;
            }"""
        )
    except Exception:
        return False


def _dump_topbar(page):
    """把视口顶部 150px 内所有可点击/有 label 的元素信息写文件并打印，便于失败兜底定位。"""
    try:
        data = page.evaluate(
            r"""() => {
                const arr = [];
                const sels = ['button', '[role="button"]', 'a', 'div', 'span'];
                for (const s of sels) {
                    for (const e of document.querySelectorAll(s)) {
                        const r = e.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const cs = getComputedStyle(e);
                        if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                        if (r.top > 150) continue;
                        const label = (e.getAttribute('aria-label') || '')
                            + ' | title:' + (e.getAttribute('title') || '')
                            + ' | text:' + ((e.textContent || '').trim().slice(0, 40));
                        arr.push('[' + e.tagName + ' role=' + (e.getAttribute('role') || '')
                            + '] top=' + Math.round(r.top) + ' left=' + Math.round(r.left) + ' ' + label);
                    }
                }
                return arr;
            }"""
        )
        log(f"  [诊断] 顶栏元素 ({len(data)}):")
        for line in data:
            log("    " + line)
        with open(_ws("delete_diag.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(data))
        log(f"  诊断已写入 {_ws('delete_diag.txt')}")
    except Exception as e:
        log(f"  _dump_topbar 失败：{e}")


def _find_select_all(page):
    """在顶栏内找 role=checkbox 的『全选』复选框。返回 element handle 或 None。"""
    try:
        handle = page.evaluate_handle(
            r"""() => {
                const cbs = Array.from(document.querySelectorAll('[role="checkbox"]'));
                for (const c of cbs) {
                    const r = c.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const cs = getComputedStyle(c);
                    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                    if (r.top > 80) continue;
                    const txt = ((c.getAttribute('aria-label') || '') + ' '
                        + (c.textContent || '')).toLowerCase();
                    if (txt.includes('全选') || txt.includes('select all')) return c;
                }
                return null;
            }"""
        )
        return handle.as_element()
    except Exception:
        return None


def _loaded_count(page):
    try:
        return page.evaluate(
            r"""() => document.querySelectorAll('a[href*="/photo/"]').length"""
        )
    except Exception:
        return 0


def _wait_tiles(page, timeout=90):
    """等待照片瓦片出现。删除一批后页面会重载并向上回填，需要给足时间，
    否则会把『还没加载完』误判成『图库已空』（这是之前 260 张就结束的根因）。"""
    end = time.time() + timeout
    n = 0
    waited = 0
    while time.time() < end:
        try:
            n = _loaded_count(page)
        except Exception:
            n = 0
        if n > 0:
            if waited:
                log(f"  等待 {waited}s 后瓦片已加载：{n} 张")
            return n
        time.sleep(2)
        waited += 2
        if waited % 20 == 0:
            log(f"  仍在等待照片加载…（已 {waited}s）")
    return n


def _unchecked_date_count(page):
    """返回当前 DOM 中未勾选的『日期分组复选框』数量（aria-label 形如
    "选择以下日期的所有照片：6月16日周二"）。Google 点它=选中那一整天所有照片。"""
    try:
        return page.evaluate(
            r"""() => Array.from(document.querySelectorAll(
                'div[role="checkbox"][aria-label^="选择以下日期的所有照片"]'
            )).filter(e => e.getAttribute('aria-checked') !== 'true').length"""
        )
    except Exception:
        return 0


def _next_unchecked_date_label(page):
    """返回下一个未勾选的日期复选框 aria-label；没有则返回 null。"""
    try:
        return page.evaluate(
            r"""() => {
                const e = Array.from(document.querySelectorAll(
                    'div[role="checkbox"][aria-label^="选择以下日期的所有照片"]'
                )).find(x => x.getAttribute('aria-checked') !== 'true');
                return e ? e.getAttribute('aria-label') : null;
            }"""
        )
    except Exception:
        return None


def _next_date_label(page, skip):
    """返回下一个『未勾选且不在 skip 黑名单里』的日期复选框 aria-label（按页面从上到下）。
    黑名单机制很关键：点过但计数没涨的日期若再点一次 = 取消勾选，
    这正是上一轮『点 148 个日期只选中 260 张』的根因。"""
    try:
        return page.evaluate(
            r"""(skip) => {
                const bad = new Set(skip || []);
                const els = Array.from(document.querySelectorAll(
                    'div[role="checkbox"][aria-label^="选择以下日期的所有照片"]'
                )).filter(e => e.getAttribute('aria-checked') !== 'true'
                            && !bad.has(e.getAttribute('aria-label')));
                els.sort((a, b) => {
                    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                    return ra.top - rb.top || ra.left - rb.left;
                });
                return els.length ? els[0].getAttribute('aria-label') : null;
            }""", list(skip)
        )
    except Exception:
        return None


def _click_date_by_label(page, lbl):
    """点击指定日期复选框：先滚进视口，再 force click，失败回退 JS click。
    不用 page.click(selector)——同名 label 可能多个，会触发 strict mode 报错。"""
    try:
        handle = page.evaluate_handle(
            r"""(l) => Array.from(document.querySelectorAll(
                    'div[role="checkbox"][aria-label^="选择以下日期的所有照片"]'
                )).find(x => x.getAttribute('aria-label') === l
                          && x.getAttribute('aria-checked') !== 'true') || null""",
            lbl,
        )
        el = handle.as_element()
        if el is None:
            return False
        try:
            el.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        try:
            el.click(force=True, timeout=3000)
            return True
        except Exception:
            try:
                page.evaluate("(e) => e.click()", el)
                return True
            except Exception:
                return False
    except Exception:
        return False


def _total_date_count(page):
    """返回当前 DOM 中『日期分组复选框』总数（含已勾选）。"""
    try:
        return page.evaluate(
            r"""() => document.querySelectorAll(
                'div[role="checkbox"][aria-label^="选择以下日期的所有照片"]'
            ).length"""
        )
    except Exception:
        return 0


def _unchecked_date_labels(page, limit=None):
    """返回所有未勾选的日期复选框 aria-label（按 DOM 垂直位置排序）。"""
    try:
        return page.evaluate(
            r"""(lim) => {
                const els = Array.from(document.querySelectorAll(
                    'div[role="checkbox"][aria-label^="选择以下日期的所有照片"]'
                )).filter(e => e.getAttribute('aria-checked') !== 'true');
                els.sort((a, b) => {
                    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                    return ra.top - rb.top || ra.left - rb.left;
                });
                return els.map(e => e.getAttribute('aria-label')).slice(0, lim || els.length);
            }""", limit
        )
    except Exception:
        return []


def select_all_photos(page, target=2000, max_scrolls=200):
    """
    进入选择态并尽量多选（用于大批量一次删除）：
      1) 进入选择态（hover 首张照片 + 点复选框）
      2) 逐个点击未勾选的『日期分组复选框』（每个 = 那一整天所有照片），
         累计到 target 或没有更多未勾选日期时停止；标签耗尽就小幅滚屏继续加载
      3) 若仍不足，兜底按 Ctrl+A
    返回选中数（失败返回 -1）。
    """
    try:
        cur = _read_selected_count(page)
        if not cur or cur <= 0:
            n0 = _bulk_select_one(page)
            if n0 <= 0:
                log("  进入选择态失败。")
                return -1
            cur = n0
            log(f"  已进入选择态，初始 {cur} 张")

        # 2) 逐个点击未勾选的日期复选框（每个 = 一整天）
        #    每次只取一个 label 并校验计数是否真的增长：
        #      涨了 -> 有效，继续；没涨 -> 拉黑该 label（再点会变成取消勾选）
        clicked = 0        # 真正生效的日期数
        skipped = 0        # 点了没生效被拉黑的
        stall = 0
        skip = set()
        while cur < target:
            lbl = _next_date_label(page, skip)
            if lbl is None:
                # 当前 DOM 里没有可点日期了，小步滚屏加载更多
                page.mouse.wheel(0, 1400)
                time.sleep(0.9)
                lbl = _next_date_label(page, skip)
                if lbl is None:
                    stall += 1
                    if stall >= 8:
                        log(f"  连续 {stall} 次滚屏未发现新日期分组，停止累积。")
                        break
                    continue
            stall = 0

            before = cur
            _click_date_by_label(page, lbl)
            # 快路径：多数情况计数立刻更新，0.3s 读到就继续下一个日期
            time.sleep(0.3)
            cur = _read_selected_count(page) or before
            if cur <= before:
                # 慢路径：再给 ~1.5s（大日期分组勾选有延迟）
                for _ in range(5):
                    time.sleep(0.3)
                    cur = _read_selected_count(page) or before
                    if cur > before:
                        break
            if cur > before:
                clicked += 1
                if clicked <= 3 or clicked % 10 == 0:
                    log(f"    +{cur - before} 张（{lbl[-12:]}），累计 {cur}")
            else:
                # 点了没涨 -> 拉黑，绝不再点（再点等于取消勾选）
                skip.add(lbl)
                skipped += 1
                if skipped > 80:
                    log("  大量日期点击无效，停止累积（避免误取消已勾选内容）。")
                    break
            if clicked + skipped > 600:
                log("  已达单轮点击上限 600，进入删除。")
                break

        cur = _read_selected_count(page) or 0
        if cur > 0:
            log(f"  日期分组选中：{clicked} 个日期生效 / {skipped} 个无效跳过，共选 {cur} 张")
            return cur

        # 3) 兜底：Ctrl+A
        try:
            page.keyboard.press("Control+a")
        except Exception:
            pass
        time.sleep(1.2)
        cur = _read_selected_count(page) or 0
        if cur > 0:
            log(f"  Ctrl+A 兜底全选：{cur} 张")
            return cur
        return -1
    except Exception as e:
        log(f"  select_all_photos 出错（已吞掉）：{e}")
        return -1


def _wait_count_zero(page, tries=40):
    """删除后轮询选择计数，归零则 True（处理计数刷新延迟 / 大批量后台搬移）。
    补充：若顶栏『已选择』整块消失（读不到计数）且确认对话框已关闭，
    说明选择态已退出=删除已提交，同样算成功，避免白等 tries 秒。"""
    gone = 0
    for _ in range(tries):
        try:
            c = _read_selected_count(page)
        except Exception:
            c = None
        if c is not None and c == 0:
            return True
        if c is None:
            gone += 1
            if gone >= 5 and not _dialog_open(page):
                log("  选择态已退出（顶栏计数消失、无对话框）→ 视为删除已提交。")
                return True
        else:
            gone = 0
        time.sleep(1.0)
    return False


def click_delete_and_confirm(page):
    """
    已选中状态下：删除（移到回收站）并确认。
    关键修复：确认对话框的遮罩（scrim）会拦截对顶栏『移至回收站』的再次点击，
    因此必须在 role=dialog 范围内点确认按钮（force click），而非再去点顶栏按钮。
    成功判定：删前选中数>0 且 删后计数归零（严格，避免假成功）。
    """
    try:
        before = _read_selected_count(page)
        if before is None or before <= 0:
            log("  删除前选中数为 0，没有可删除的内容（选择可能已丢失）。")
            return False
        log(f"  删除前选中数 = {before}")

        # 1) 打开确认对话框（若尚未打开）：点顶栏『移至回收站』
        confirmed = False
        for attempt in range(3):
            if not _dialog_open(page):
                log("  点击顶栏『移至回收站』打开确认对话框…")
                if not _click_toolbar_trash(page):
                    log("  未能点击顶栏删除按钮。")
                    _dump_topbar(page)
                    return False
                time.sleep(1.5)
            if _confirm_dialog(page):
                confirmed = True
                break
            # 兜底：对话框内按 Enter 确认
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            time.sleep(1.5)
            if not _dialog_open(page):
                confirmed = True
                break
        if not confirmed:
            log("  未能确认删除对话框。")
            _dump_topbar(page)
            return False

        # 2) 判定：选择计数归零 = 删除成功
        if _wait_count_zero(page, tries=60):
            log("  删除成功（选择计数归零）。")
            return True

        # 3) 若对话框仍开（确认没生效），再试一次确认
        if _dialog_open(page) and _confirm_dialog(page):
            if _wait_count_zero(page, tries=60):
                log("  删除成功（二次确认）。")
                return True

        _dump_topbar(page)
        log("  删除路径失败，本轮停止。")
        return False
    except Exception as e:
        log(f"  click_delete_and_confirm 出错（已吞掉）：{e}")
        try:
            _dump_topbar(page)
        except Exception:
            pass
        return False


def scroll_to_load_more(page, max_scrolls=8):
    """向下滚屏触发 Google 相簿无限滚动加载更多瓦片。"""
    try:
        prev_count = page.evaluate(
            r"""() => {
                const sels = ['grid-item','div[role="button"][data-testid]',
                    'div[jsaction*="click"][aria-label]','a[href*="/photo/"]'];
                let n = 0;
                for (const s of sels) n += document.querySelectorAll(s).length;
                return n;
            }"""
        )
        for i in range(max_scrolls):
            page.mouse.wheel(0, 2000)
            time.sleep(0.8)
        time.sleep(1.2)
        new_count = page.evaluate(
            r"""() => {
                const sels = ['grid-item','div[role="button"][data-testid]',
                    'div[jsaction*="click"][aria-label]','a[href*="/photo/"]'];
                let n = 0;
                for (const s of sels) n += document.querySelectorAll(s).length;
                return n;
            }"""
        )
        log(f"  滚屏加载：{prev_count} → {new_count} 张")
        return new_count
    except Exception as e:
        log(f"  滚屏失败：{e}")
        return -1


def main():
    from playwright.sync_api import sync_playwright

    global PROXY, CDP_PORT, CHROME_EXE
    CHROME_EXE = os.environ.get("GPBD_CHROME_EXE") or detect_chrome_exe()
    if not CHROME_EXE or not os.path.exists(CHROME_EXE):
        sys.exit("未找到 Chrome 可执行文件，请设置环境变量 GPBD_CHROME_EXE 指向 chrome.exe / Google Chrome.app。")
    PROXY = detect_proxy()

    if chrome_running():
        log("检测到 Chrome 正在运行（含后台进程）。")
        log("   请先彻底关闭： taskkill /F /IM chrome.exe   然后重跑。")
        sys.exit("Chrome 未关闭，已中止。")
    if not check_proxy(PROXY):
        sys.exit("代理不可用，已中止。")
    if DRY_RUN is False and not BACKUP_DONE:
        log("真实删除前请先把 BACKUP_DONE 改为 True（并确实已去 takeout.google.com 备份）。")
        sys.exit("未确认备份，已中止。")

    clear_singleton_lock()
    ensure_junction()

    proc = None
    clean_exit = False
    try:
        CDP_PORT = _free_port()
        proc = launch_chrome_cdp(PROXY, CDP_PORT)

        ver = chrome_cdp_ready(CDP_PORT)
        if not ver:
            raise RuntimeError(f"Chrome 调试端口 {CDP_PORT} 未就绪（可能启动失败或被旧实例接管）")

        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            except Exception as e:
                log(f"CDP 连接失败：{e}")
                log("   常见原因：Chrome 未真正开启调试端口，或 --remote-allow-origins 未生效。")
                raise
            log(f"CDP 已连接；contexts 数量={len(browser.contexts)}")

            context = None
            if browser.contexts:
                context = browser.contexts[0]
                log(f"使用真实 profile context（pages={len(context.pages)}）")
            else:
                log("⚠️ 未检测到真实 profile context，新建（可能无登录态，仅供诊断）。")
                context = browser.new_context()

            page = None
            for pg in context.pages:
                if "photos.google.com" in (pg.url or ""):
                    page = pg
                    break
            if page is None:
                if context.pages:
                    page = context.pages[0]
                else:
                    page = context.new_page()
                log(f"新建/复用 page 导航到 {TARGET_URL} …")
                safe_goto(page, TARGET_URL)

            entered = False
            for i in range(30):
                url = page.url or ""
                title = page.title() or ""
                has_photos = page.evaluate(
                    r"""() => document.querySelectorAll('a[href*="/photo/"]').length > 0"""
                )
                log(f"  轮询({i*2}s) URL={url} 标题={title} photos={has_photos}")
                if ("signin" in url) or ("ServiceLogin" in url):
                    break
                if ("无法连接" in title) or ("ERR_" in title) or ("This site can't be reached" in title) or ("This webpage is not available" in title):
                    log("  检测到错误页，刷新重试…")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        log(f"  reload 失败：{e}")
                    time.sleep(2)
                    continue
                if title.startswith("Loading") or title == "":
                    time.sleep(2)
                    continue
                if ("photos.google.com" in url) and has_photos:
                    entered = True
                    break
                time.sleep(2)
            _safe_screenshot(page, "step1_loaded.png")
            log("已保存截图 step1_loaded.png（可在工作目录查看）。")

            if not entered:
                log("⚠️ 未进入图库（可能停在登录页/介绍页）。")
                log("   若是登录页：请在弹出的 Chrome 窗口里手动登录 Google，然后关闭 Chrome 重跑脚本。")
                log("   脚本结束，Chrome 保持打开供你登录。")
                clean_exit = True
                return

            n = count_tiles(page)
            log(f"[诊断] 当前页面候选照片瓦片数 = {n}")

            if DRY_RUN:
                for trial in range(3):
                    log(f"--- DRY_RUN 第 {trial+1}/3 轮：滚屏+全选验证 ---")
                    scroll_to_load_more(page, max_scrolls=10)
                    sel = bulk_select_visible(page)
                    log(f"  本轮 bulk_select 结果：{sel} 张")
                    time.sleep(1.0)
                _safe_screenshot(page, "step2_selected.png")
                log(f"【DRY_RUN】完成验证，截图已保存 step2_selected.png。请在 Chrome 窗口里肉眼确认是否勾选上大量照片。")
                log("脚本结束（DRY_RUN）。查看截图；关闭 Chrome 后可继续下一步。")
                clean_exit = True
                return

            # ---- 真实删除 ----
            deleted_total = 0
            rounds = 0
            empty_strikes = 0        # 连续「未选中」次数，达到 3 且页面确无瓦片才判定图库已空
            MAX_ROUNDS = 5000
            if FULL_DELETE:
                log(f"【全量删除模式】每批累积选中约 {BATCH_TARGET} 张后一次删除。")
                log("   即将开始全量删除；按 Enter 开始 / Ctrl+C 中止（开始后每批自动进行，可随时 Ctrl+C 停）。")
                try:
                    input(">>> ")
                except (EOFError, KeyboardInterrupt):
                    log("用户中止。")
                    clean_exit = True
                    return
            while True:
                rounds += 1
                if not FULL_DELETE and deleted_total >= TEST_BATCH:
                    log(f"已删 {deleted_total} 张，达到 TEST_BATCH={TEST_BATCH}，停止。")
                    break
                if rounds > MAX_ROUNDS:
                    log(f"超过安全轮次 {MAX_ROUNDS}，停止。")
                    break

                log(f"--- 第 {rounds} 轮：全选至 ~{BATCH_TARGET} 张 ---")
                sel = select_all_photos(page, target=BATCH_TARGET)
                log(f"  本轮选中：{sel} 张")
                if sel <= 0:
                    # 关键修复：删完一批后页面回填较慢，直接判"空"会提前结束（实际还有上万张）。
                    empty_strikes += 1
                    tiles = _loaded_count(page)
                    log(f"  本轮未选中（连续第 {empty_strikes}/3 次），当前瓦片数={tiles}")
                    if empty_strikes >= 3 and tiles == 0:
                        log("  连续 3 轮未选中且页面无任何照片 → 判定图库已空。结束。")
                        break
                    if empty_strikes >= 6:
                        log("  连续 6 轮未选中，为安全起见停止（可重跑脚本继续）。")
                        break
                    log("  刷新页面并等待照片加载后重试…")
                    try:
                        safe_goto(page, TARGET_URL)
                    except Exception as e:
                        log(f"  刷新失败：{e}")
                    _wait_tiles(page, timeout=90)
                    time.sleep(3)
                    continue
                empty_strikes = 0

                # 试删模式：删前人工确认（可手动缩小范围）
                if not FULL_DELETE:
                    log(f"【试删模式】本批将删除约 {sel} 张。")
                    log("   如需缩小范围，可在 Chrome 窗口里手动取消部分勾选；")
                    log("   确认无误后按 Enter 继续删除，或按 Ctrl+C 中止。")
                    try:
                        input(">>> 按 Enter 继续 / Ctrl+C 中止：")
                    except (EOFError, KeyboardInterrupt):
                        log("用户中止删除流程。")
                        clean_exit = True
                        return

                ok = click_delete_and_confirm(page)
                if not ok:
                    # 重试一次：重新累积选中 + 删除
                    log("  删除失败，重试一次（重新全选 + 删除）…")
                    sel2 = select_all_photos(page, target=BATCH_TARGET)
                    if sel2 <= 0:
                        log("  重试时未能重新选中，停止。")
                        break
                    ok = click_delete_and_confirm(page)
                    if not ok:
                        log("  重试仍失败，停止。")
                        break

                deleted_total += sel
                log(f"  本轮完成 +{sel}，累计约 {deleted_total} 张")

                time.sleep(BATCH_PAUSE)
                try:
                    safe_goto(page, TARGET_URL)
                except Exception as e:
                    log(f"  刷新失败（继续尝试下一轮）：{e}")
                got = _wait_tiles(page, timeout=90)
                log(f"  刷新后已加载 {got} 张瓦片，准备下一轮。")
                time.sleep(2)

            log(f"删除流程结束。累计约 {deleted_total} 张已移到回收站（60 天内可在 photos.google.com 回收站恢复）。")
            log("如需彻底清空，去 photos.google.com 回收站 → 「清空回收站」。")
    finally:
        if proc and proc.poll() is None and not clean_exit:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass


if __name__ == "__main__":
    main()
