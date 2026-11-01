# -*- coding: utf-8 -*-
"""把 DeepSeek 接到个人微信（Windows 客户端 + wechatauto）。

原理：
    读消息 → 解密本地 SQLCipher 数据库轮询（只读，不注入进程）
    发消息 → UIA 控件树 / 坐标+OCR 驱动真实客户端（会抢占窗口焦点）

⚠️ 依赖微信客户端本身，微信升级后可能失效；发送时会抢占窗口焦点。

用法：
    python wx_bot.py --check      # 自检：能不能读库、能不能拿到账号、DeepSeek 通不通
    python wx_bot.py --dry-run    # 只听不回，先把判定逻辑跑一遍
    python wx_bot.py              # 正式运行
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import re
import shutil
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

# pywin32 用 --target 装到非 site-packages 时，.pth 不会被执行，
# 这个模块负责把 pywin32_system32 加进 DLL 搜索路径。正常安装时它不存在。
try:
    import pywin32_bootstrap  # noqa: F401
except ImportError:
    pass

# Windows 控制台默认 GBK，中文会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from brain import (  # noqa: E402
    AFFINITY_INIT,
    IMAGE_TYPE,
    PROACTIVE_NOTE,
    REFLECT_SYSTEM,
    SUMMARY_SYSTEM,
    TEXT_LIMIT,
    Config,
    DeepSeekClient,
    DeepSeekError,
    EchoGuard,
    RateLimiter,
    SessionStore,
    UnsupportedImage,
    WEB_SEARCH_TOOL,
    WebSearch,
    clean_reply,
    clean_summary,
    decide,
    encode_image,
    extract_affinity,
    extract_kaomoji,
    extract_moments,
    extract_notes,
    extract_stickers,
    format_user_content,
    list_stickers,
    load_config,
    parse_reflection,
    pick_kaomoji,
    render_dialogue,
    resolve_sticker,
    should_voice,
    speakable,
    split_messages,
    split_text,
    strip_sender_prefix,
    strip_unknown_tags,
    tier_of,
)

import voice as voice_lib  # noqa: E402  合成 + 定点播放（winmm waveOut）

log = logging.getLogger("wxbot")

_SENTINEL = object()   # 发送线程的退出信号

# 纯笑声 / 纯凑数的消息：主动说话不值得为它花一次 API，本地就掐掉
_NOISE = re.compile(
    r"^(?:哈+|嘿+|呵+|嘻+|h+|23{2,}|6{3,}|草+|笑死(?:我了)?|乐死|绝了|牛+"
    r"|[嗯哦啊噢呃]+)[\s,，.。!！?？~～…、]*$",
    re.I,
)


def apply_rhythm(cfg: Config) -> None:
    """套用 wechatauto 的「拟人节奏」档位。

    `natural`（库的默认）会让每次写操作之间强行等 2.5~6 秒，120 秒内最多 6 次，
    撞上限还要冷却 30~75 秒——机器人回复会显得非常磨叽。
    `off` 则完全不加这层节流。
    """
    name = (cfg.rhythm_profile or "off").strip().lower()
    try:
        from wechatauto import rhythm

        p = rhythm.set_profile(name)
        log.info("拟人节奏：%s（写操作最小间隔 %s 秒，突发上限 %s）",
                 p.name, p.gap, p.burst or "不限")
    except Exception as exc:
        log.warning("设置拟人节奏失败（%r），沿用库的默认档位", exc)


def patch_image_paste(wait: float = 3.0) -> None:
    """把 wechatauto 的「图片草稿检测」换成固定等待。

    它原本靠截屏数输入框区域里的彩色像素，来判断图片有没有粘上去。实测在微信
    4.1.15 上经常判成 False，于是循环变成：
        粘上 → 判 False → Ctrl+A/Delete 清掉 → 再粘 → 判 False → …… ×3
    一次要 25 秒，而且最后一轮可能在没粘上的状态下回车，结果什么都没发出去
    （数据库核对：3 次「已发表情」只有 1 条真的落库）。

    粘贴本身没问题，所以这里改成「粘一次 → 等 wait 秒 → 回车」。
    """
    try:
        from wechatauto import guia
    except Exception as exc:            # 没装 wechatauto 时（比如跑测试）直接跳过
        log.debug("跳过图片粘贴补丁：%r", exc)
        return

    cls = guia.WeChatGUI
    if getattr(cls, "_dsh_paste_patched", False):
        return

    def _patched(self, box=None, wait_=8.0, **kw):
        # 调用方是 (box, wait=6.0)，必须吃掉 wait 这个关键字
        time.sleep(max(0.5, wait))
        return True

    cls._input_box_has_color_draft = _patched
    cls._dsh_paste_patched = True
    log.info("已替换图片草稿检测为固定等待 %.1fs", wait)


# ────────────────────────────── 日志 ──────────────────────────────


def setup_logging(cfg: Config) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    if not cfg.log_file:
        return

    file_fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s")
    # 加机器人是长期挂着的，日志必须轮转：不然一年能长到几十 MB，
    # 而且老日志里全是聊天内容，留得越久越不合适。
    if int(cfg.log_max_bytes or 0) > 0:
        fh = RotatingFileHandler(
            cfg.log_file,
            maxBytes=int(cfg.log_max_bytes),
            backupCount=max(0, int(cfg.log_backups)),
            encoding="utf-8",
        )
    else:
        fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
    fh.setFormatter(file_fmt)
    log.addHandler(fh)


# ────────────────────────── 识图临时文件清理 ──────────────────────────

# 别人发来的每张图都会落一份到 _images/，只增不减会一直涨（还带着隐私）。
IMG_MIN_AGE = 600      # 刚下载的图可能正在编码，10 分钟内不删
# 取一张图最多等几秒。底层库在「等用户点开大图抓 AES 密钥」上会卡到 120 秒，
# 卡住的是回复线程，所以这里给个上限（见 Bot._download_image）。
IMAGE_FETCH_WAIT = 20.0


def prune_images(img_dir: str, keep_days: int = 7, keep_max: int = 200,
                 now: float | None = None) -> int:
    """清理识图落盘的临时图片，返回删掉的张数。

    两条规则：超过 keep_days 天的删掉；剩下的还超过 keep_max 张时，删最旧的。
    keep_max <= 0 表示不限数量（只按时间清）。
    """
    try:
        names = os.listdir(img_dir)
    except OSError:
        return 0            # 目录还不存在，没什么可清的

    now = time.time() if now is None else now
    alive: list[tuple[float, str]] = []
    removed = 0

    for name in names:
        path = os.path.join(img_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue

        if now - mtime > keep_days * 86400:
            try:
                os.remove(path)
                removed += 1
                continue
            except OSError:
                pass            # 删不掉就留着，下次再说
        alive.append((mtime, path))

    if keep_max > 0 and len(alive) > keep_max:
        alive.sort()
        for mtime, path in alive[: len(alive) - keep_max]:
            if now - mtime < IMG_MIN_AGE:
                continue        # 可能是这一轮正在识图的那张
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def prune_voice(voice_dir: str, keep_days: int = 3, keep_max: int = 50,
                now: float | None = None) -> int:
    """清理合成语音留下的 wav，返回删掉的个数。

    正常情况下发完就删（见 Bot._send_voice）；剩下的都是「合成成功但发送失败」
    或中途退出的残留，外加一堆 0 字节的空文件。不清的话会一直堆在 _voice/ 里。
    """
    try:
        names = os.listdir(voice_dir)
    except OSError:
        return 0            # 还没发过语音

    now = time.time() if now is None else now
    alive: list[tuple[float, str]] = []
    removed = 0
    for name in names:
        if not name.lower().endswith((".wav", ".mp3")):
            continue
        path = os.path.join(voice_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        try:
            if os.path.getsize(path) < 1000:    # 合成失败留下的空壳（几个字节），没任何用
                os.remove(path)
                removed += 1
                continue
        except OSError:
            pass
        if now - mtime > keep_days * 86400:
            try:
                os.remove(path)
                removed += 1
                continue
            except OSError:
                pass
        alive.append((mtime, path))

    if keep_max > 0 and len(alive) > keep_max:
        alive.sort()
        for _mtime, path in alive[: len(alive) - keep_max]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


# ─────────────────── 识图：把图片密钥存下来 ───────────────────
# 图片是加密的，密钥只能从 WeChat 进程内存里那一小块 cfg 结构派生出来
# （cfg_dword + wxid 算 md5）。实测这个过程**时灵时不灵**：同一台机器上
# 10-31 18:19 能派生、17:58 就派生不出来，只能退回「你在微信里点开一张大图
# 让我抓密钥」的慢路径。更麻烦的是派生的结果从来不落盘，于是每次重启都要
# 重新赌一次运气。
#
# 这里补上：派生成功就存到 _images/image_key.json，下次启动先读它，
# 校验（拿真实 .dat 反解一次）通过就直接用，不再依赖内存扫描。

IMAGE_KEY_FILE = "image_key.json"


def read_saved_image_key(img_dir: str) -> str:
    """读磁盘上存过的图片 AES 密钥（没有就返回空串）。"""
    path = os.path.join(img_dir, IMAGE_KEY_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return ""
    return str(data.get("aes") or "")


def save_image_key(img_dir: str, aes_key: str, cfg_dword: int = 0) -> bool:
    """把图片密钥存盘，返回是否写成。"""
    if not aes_key:
        return False
    path = os.path.join(img_dir, IMAGE_KEY_FILE)
    try:
        os.makedirs(img_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"aes": aes_key, "cfg_dword": int(cfg_dword or 0)}, f, indent=2)
        return True
    except OSError as exc:
        log.debug("存图片密钥失败：%r", exc)
        return False


def ensure_image_key(db, img_dir: str) -> str:
    """确保能拿到图片密钥，返回 AES 密钥（拿不到返回空串）。

    顺序：磁盘上存过的（先用真实密文校验）→ 从 db.cfg_dword 派生 → 失败。
    派生成功就落盘，下次不用再赌内存扫描。
    """
    try:
        from wechatauto import MediaDownloader
    except Exception as exc:
        log.debug("没装 wechatauto，跳过图片密钥检查：%r", exc)
        return ""

    try:
        md = MediaDownloader(db, save_dir=img_dir)
    except Exception as exc:
        log.debug("构造 MediaDownloader 失败：%r", exc)
        return ""

    saved = read_saved_image_key(img_dir)
    if saved:
        try:
            if md._validate_key(saved):
                return saved
            log.warning("存下的图片密钥校验没过（换账号/微信重装过？），重新派生")
        except Exception as exc:
            log.debug("校验存下的图片密钥失败：%r", exc)

    # 库里自己也可能存过（「点开大图」慢路径抓到的），一并接管过来
    try:
        lib_key = md._load_persisted_key()
    except Exception as exc:
        log.debug("读库里的图片密钥失败：%r", exc)
        lib_key = None
    if lib_key:
        save_image_key(img_dir, lib_key, getattr(db, "cfg_dword", 0) or 0)
        log.info("图片密钥：沿用库里抓到的密钥（已存到 %s）",
                 os.path.join(img_dir, IMAGE_KEY_FILE))
        return lib_key

    if not getattr(db, "cfg_dword", None):
        log.warning("图片密钥：拿不到 cfg_dword（微信升级/权限/内存扫描失败），"
                    "识图会走「等你点开大图」的慢路径")
        return ""

    try:
        derived = md._derive_cfg_key()
    except Exception as exc:
        log.debug("派生图片密钥失败：%r", exc)
        derived = None
    if not derived:
        log.warning("图片密钥：cfg_dword=%s 但派生/校验失败，识图可能用不了",
                    getattr(db, "cfg_dword", None))
        return ""

    aes_key = derived[0]
    if save_image_key(img_dir, aes_key, getattr(db, "cfg_dword", 0) or 0):
        log.info("图片密钥：已从 cfg_dword 派生并存到 %s",
                 os.path.join(img_dir, IMAGE_KEY_FILE))
    return aes_key


# ─────────────────── 多开微信：锁定账号、锁定窗口 ───────────────────
# 电脑上同时登着两个微信时，底层库有两个坑：
#   1. 读消息：`WeChatDB()` 靠「最近改动过的库」猜账号，两个号都活跃时会猜错，
#      于是机器人读着 A 的聊天、你还以为它没收到消息；更糟的是猜错后要重新
#      提取密钥（扫内存 + 每个库 25 万次 PBKDF2），启动能卡好几分钟。
#   2. 发消息：guia 用 hwnd 定位坐标，但 UIA 引擎自己会重扫所有微信窗口、
#      挑面积最大的那个——两个窗口就变成「在这边看会话、往那边粘字」。
# 所以这里做两件事：用 wx_account 钉死账号，用 wx_hwnd（或自动匹配）钉死窗口。

_HWND_PIN_LOCK = threading.Lock()
_MUTEX_HANDLE = None      # 单实例互斥量的句柄（进程退出自动释放）


def acquire_single_instance(account: str) -> bool:
    """同一个微信号只允许一个机器人进程。

    两个实例同时跑会抢同一个微信窗口、抢同一份监听水位，表现就是
    「有时候回、有时候不回」。用命名互斥量挡掉第二个。
    系统不给建（权限/沙箱）时不拦着用户，直接放行。
    """
    global _MUTEX_HANDLE
    name = "Local\\wx-deepseek-" + (account or "default")
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, name)
        if not handle:
            return True
        if k32.GetLastError() == 183:       # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception as exc:
        log.debug("单实例检查跳过：%r", exc)
        return True


def list_account_dirs(db_dir: str) -> list[str]:
    """列出微信数据目录下的账号目录名（形如 wxid_xxx_e1f1）。"""
    if not db_dir:
        return []
    try:
        from wechatauto.db import _find_account_dirs

        return [os.path.basename(d) for d in _find_account_dirs(db_dir)]
    except Exception as exc:
        log.debug("枚举微信账号目录失败：%r", exc)
        return []


def wxid_of_account(dir_name: str) -> str:
    """账号目录名去掉末尾 4 位哈希，得到 wxid。"""
    return re.sub(r"_[A-Za-z0-9]{4}$", "", str(dir_name or ""))


def resolve_account(wanted: str, dirs) -> str:
    """把 wx_account 配置对到账号目录名；对不上返回空串。

    支持三种写法：完整目录名（wxid_xxx_e1f1）、wxid（wxid_xxx）、
    目录名大小写不敏感的前缀。
    """
    want = str(wanted or "").strip().lower()
    if not want:
        return ""
    names = [str(d) for d in (dirs or [])]
    for name in names:
        if name.lower() == want:
            return name
    for name in names:
        if wxid_of_account(name).lower() == want:
            return name
    for name in names:
        if name.lower().startswith(want):
            return name
    return ""


def _heartbeat(label: str, interval: float = 20.0) -> threading.Event:
    """干一件可能很慢的事时，每隔一会儿打一行日志。

    打开微信库有可能卡在「重新提取密钥」上（扫内存 + 每个库 25 万次 PBKDF2），
    几分钟里日志一片安静，看着就像死了。有这行至少知道它在干活。
    返回的 Event set() 一下就停。
    """
    stop = threading.Event()

    def _run():
        started = time.time()
        while not stop.wait(interval):
            log.warning("%s还在进行（已 %.0f 秒）——多半是在重新提取微信密钥，"
                        "首次启动 / 换账号 / 微信升级后会这样，等它跑完",
                        label, time.time() - started)

    threading.Thread(target=_run, name="wx-heartbeat", daemon=True).start()
    return stop


def open_wechat_db(cfg: Config):
    """打开微信数据库，返回 (db, self_info)。

    配置了 `wx_account` 就只认那个账号：找不到直接报错并列出本机账号，
    绝不退回「猜一个」——猜错的表现就是「刚启动不回消息」。
    """
    from wechatauto.db import WeChatDB, auto_detect_db_dir

    wanted = (cfg.wx_account or "").strip()
    started = time.time()
    db_dir = ""
    dirs: list[str] = []
    try:
        db_dir = auto_detect_db_dir() or ""
        dirs = list_account_dirs(db_dir)
    except Exception as exc:        # 找不到目录时交给 WeChatDB 自己报错
        log.debug("定位微信数据目录失败：%r", exc)

    account = resolve_account(wanted, dirs)
    if wanted and not account:
        raise RuntimeError(
            f"配置里锁定的微信账号「{wanted}」对不上任何账号目录。"
            f"本机现有：{'、'.join(dirs) or '（没找到任何账号目录）'}。"
            "wx_account 只认 wxid 或账号目录名（启动日志里会打印），昵称不行。")

    kwargs = {}
    if db_dir:
        kwargs["db_dir"] = db_dir
    if account:
        kwargs["account"] = account
    beat = _heartbeat("打开微信数据库")
    try:
        db = WeChatDB(**kwargs)
    finally:
        beat.set()
    info = db.get_self_info()

    nick = info.get("nick_name") or ""
    wxid = info.get("username") or ""
    log.info("微信数据库就绪（%.1fs）：%s（%s）", time.time() - started,
             nick or "?", wxid or "?")

    others = [d for d in dirs if d != getattr(db, "account", "")]
    if account:
        if others:
            log.info("已锁定微信账号：%s；忽略另外 %d 个账号目录（%s）",
                     db.account, len(others), "、".join(others))
        else:
            log.info("已锁定微信账号：%s", db.account)
    elif len(dirs) > 1:
        log.warning(
            "检测到 %d 个微信账号目录（%s），当前自动选了 %s。"
            "同时登着两个微信时请在 config.json 里填 wx_account 锁定，"
            "否则可能读到另一个账号的聊天、或者启动时重算密钥卡很久。",
            len(dirs), "、".join(dirs), getattr(db, "account", "?"))
    return db, info


def _window_rect(hwnd: int):
    try:
        import ctypes
        import ctypes.wintypes as wt

        r = wt.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return 0, 0, 0, 0
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        return 0, 0, 0, 0


def _window_pid(hwnd: int) -> int:
    try:
        import ctypes
        import ctypes.wintypes as wt

        pid = wt.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _is_window(hwnd: int) -> bool:
    try:
        import ctypes

        return bool(hwnd) and bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False


def restore_window(hwnd: int, gui=None) -> bool:
    """把最小化 / 收进托盘的微信主窗口拉回来，返回「本来需要还原」与否。

    为什么不能只靠库里的 ``ensure_visible()``：它带 15 秒缓存，窗口在缓存有效
    期内被最小化的话它直接返回 True，不再碰窗口；接着 UIA 粘贴和回车就全打在
    一个最小化的窗口上，消息静默发不出去（表现为「收到消息但不回」）。

    三种「不见了」的形态都要处理：
      · 最小化（IsIconic）→ SW_RESTORE
      · 隐藏（点了关闭进托盘）→ SW_SHOW
      · 没最小化但被挪到屏幕外（-32000 那种托盘停放位）→ 挪回可见区域
    ``ShowWindow`` 对某些窗口不生效（返回 0 且状态不变），这时再借库里那套
    ``bring_to_front()``（置顶 + 抢前台，带 AttachThreadInput）补一次。
    """
    if not hwnd:
        return False
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    h = ctypes.c_void_p(int(hwnd))
    if not u.IsWindow(h):
        return False

    native = False
    try:
        if u.IsIconic(h):
            u.ShowWindow(h, 9)          # SW_RESTORE
            native = True
        elif not u.IsWindowVisible(h):
            u.ShowWindow(h, 5)          # SW_SHOW
            native = True

        rect = wintypes.RECT()
        if u.GetWindowRect(h, ctypes.byref(rect)):
            if rect.left <= -30000 or rect.top <= -30000:
                u.ShowWindow(h, 5)
                u.SetWindowPos(h, None, 120, 120, 0, 0, 0x0001 | 0x0040)  # NOSIZE|SHOWWINDOW
                native = True
    except Exception as exc:
        log.debug("还原微信窗口失败：%r", exc)

    if not native:
        return False

    time.sleep(0.35)
    still_min = bool(u.IsIconic(h))
    if still_min and gui is not None:
        # ShowWindow 没拉动，交给库里的置顶/抢前台流程再试一次
        try:
            gui.bring_to_front(keep_topmost=False)
            time.sleep(0.4)
        except Exception as exc:
            log.debug("bring_to_front 失败：%r", exc)
        still_min = bool(u.IsIconic(h))

    if still_min:
        log.warning("微信窗口是最小化的，还原没成功（hwnd=%s）——消息可能发不出去", hwnd)
        return True
    log.info("微信窗口原先是最小化/收起的，已拉回来（hwnd=%s）", hwnd)
    return True


def wechat_windows() -> list[dict]:
    """当前可见的微信主窗口，按面积从大到小：{hwnd, pid, w, h}。

    直接用底层库自己的筛选（标题像主窗 + 加载了 Weixin.dll），
    保证和我们一会儿要钉的窗口是同一批，不会出现「选到一个发不了消息的窗口」。
    """
    try:
        from wechatauto.uia_driver import WeChatUIA

        hwnds = WeChatUIA()._wechat_hwnds()
    except Exception as exc:
        log.debug("枚举微信窗口失败：%r", exc)
        return []

    out = []
    for hwnd in hwnds:
        left, top, right, bottom = _window_rect(hwnd)
        out.append({
            "hwnd": int(hwnd),
            "pid": _window_pid(hwnd),
            "w": max(0, right - left),
            "h": max(0, bottom - top),
        })
    out.sort(key=lambda w: w["w"] * w["h"], reverse=True)
    return out


def _processes_using(path: str) -> list[int]:
    """哪些进程打开着这个文件（Windows Restart Manager，不需要管理员）。

    多开微信时每个账号是独立进程，靠「谁开着这个账号的 message.db」就能把
    账号和窗口对上。系统不让查（沙箱/精简系统）时返回空列表，调用方退回启发式。
    """
    import ctypes
    import ctypes.wintypes as wt

    class _UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wt.DWORD), ("ProcessStartTime", wt.FILETIME)]

    class _PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", _UNIQUE_PROCESS),
            ("strAppName", wt.WCHAR * 256),
            ("strServiceShortName", wt.WCHAR * 64),
            ("ApplicationType", ctypes.c_int),
            ("AppStatus", wt.ULONG),
            ("TSSessionId", wt.DWORD),
            ("bRestartable", wt.BOOL),
        ]

    try:
        lib = ctypes.WinDLL("rstrtmgr")
        lib.RmStartSession.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, wt.LPWSTR]
        lib.RmStartSession.restype = wt.DWORD
        session = wt.DWORD(0)
        key = ctypes.create_unicode_buffer(33)      # CCH_RM_SESSION_KEY + 1
        if lib.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            names = (wt.LPCWSTR * 1)(path)
            if lib.RmRegisterResources(session, 1, names, 0, None, 0, None) != 0:
                return []
            needed = wt.UINT(0)
            have = wt.UINT(0)
            reasons = wt.DWORD(0)
            rc = lib.RmGetList(
                session, ctypes.byref(needed), ctypes.byref(have),
                None, ctypes.byref(reasons))
            if rc not in (0, 234) or not needed.value:      # 234 = MORE_DATA
                return []
            buf = (_PROCESS_INFO * needed.value)()
            have = wt.UINT(needed.value)
            if lib.RmGetList(session, ctypes.byref(needed), ctypes.byref(have),
                             buf, ctypes.byref(reasons)) != 0:
                return []
            return [int(buf[i].Process.dwProcessId) for i in range(have.value)]
        finally:
            lib.RmEndSession(session)
    except Exception as exc:
        log.debug("查文件占用失败（%s）：%r", os.path.basename(path), exc)
        return []


def account_window_pid(account_dir: str) -> int:
    """这个账号的库正被哪个进程打开；查不到返回 0。"""
    if not account_dir:
        return 0
    base = os.path.join(account_dir, "db_storage")
    files = []
    try:
        for root, _dirs, names in os.walk(base):
            for name in names:
                if name.endswith(".db") and not name.endswith(("-wal", "-shm")):
                    files.append(os.path.join(root, name))
    except OSError:
        return 0
    # 消息库最常被写，先查它；再逐个试，命中就停
    files.sort(key=lambda p: 0 if "message" in p.lower() else 1)
    for path in files[:8]:
        pids = _processes_using(path)
        if pids:
            return pids[0]
    return 0


def pick_send_window(cfg: Config, db=None) -> int:
    """挑一个发送窗口，返回 hwnd（0 = 交给底层库自己找）。

    优先级：配置里的 wx_hwnd > 只有一个窗口就用它 > 按「谁开着这个账号的库」
    匹配进程 > 面积最大的那个（并在日志里说清楚为什么不确定）。
    """
    wins = wechat_windows()
    if not wins:
        return 0

    pinned = int(getattr(cfg, "wx_hwnd", 0) or 0)
    if pinned:
        hit = [w for w in wins if w["hwnd"] == pinned]
        if hit:
            log.info("发送窗口：按 wx_hwnd 锁定 hwnd=%s（pid=%s）",
                     pinned, hit[0]["pid"])
            return pinned
        log.warning("配置的 wx_hwnd=%s 不是当前可见的微信窗口（可能微信重开过），"
                    "改为自动选择", pinned)

    if len(wins) == 1:
        log.info("发送窗口：hwnd=%s（pid=%s，只有一个微信窗口）",
                 wins[0]["hwnd"], wins[0]["pid"])
        return wins[0]["hwnd"]

    pid = account_window_pid(getattr(db, "account_dir", "") or "")
    if pid:
        hit = [w for w in wins if w["pid"] == pid]
        if len(hit) == 1:
            log.info("发送窗口：hwnd=%s（pid=%s，账号 %s 的数据库由它打开）",
                     hit[0]["hwnd"], pid, getattr(db, "account", "?"))
            return hit[0]["hwnd"]

    log.warning(
        "检测到 %d 个微信窗口（%s），但没法确认哪个是账号 %s 的，"
        "先按面积最大的那个发。若发现回错了号，请用 --wx-list 看窗口，"
        "把对应 hwnd 填进 config.json 的 wx_hwnd。",
        len(wins), "、".join(f"hwnd={w['hwnd']}/pid={w['pid']}" for w in wins),
        getattr(db, "account", "?") or "?")
    return wins[0]["hwnd"]


def pin_uia_window(gui, hwnd: int) -> None:
    """让 UIA 引擎只认这一个窗口。

    guia 用 hwnd 算坐标，但 uia_driver 每次会自己重扫所有微信窗口、取面积
    最大的那个。多开时两者可能不是同一个窗口 —— 表现为「当前会话看着对，
    消息粘到了另一个微信里」。这里把引擎的窗口列表固定成我们选的那一个。
    """
    if not hwnd or gui is None:
        return
    original = getattr(gui, "_get_uia", None)
    if original is None:
        return

    def _get(refresh: bool = False):
        eng = original(refresh)
        if eng is not None and getattr(eng, "_dsh_pinned_hwnd", None) != hwnd:
            scan = eng._wechat_hwnds

            def _only(diag=None):
                hits = scan(diag)
                return [hwnd] if _is_window(hwnd) else hits

            eng._wechat_hwnds = _only
            eng._dsh_pinned_hwnd = hwnd
        return eng

    with _HWND_PIN_LOCK:
        gui._get_uia = _get


def wx_list(cfg: Config) -> int:
    """--wx-list：打印微信窗口和账号目录，方便手工锁定。"""
    from wechatauto.db import auto_detect_db_dir

    db_dir = ""
    dirs: list[str] = []
    try:
        db_dir = auto_detect_db_dir() or ""
        dirs = list_account_dirs(db_dir)
    except Exception as exc:
        print(f"定位微信数据目录失败：{exc}")

    print(f"微信数据目录：{db_dir or '（没找到）'}")
    print("账号目录：")
    if not dirs:
        print("  （没有）")
    for name in dirs:
        print(f"  {name}")

    print()
    print("微信窗口：")
    wins = wechat_windows()
    if not wins:
        print("  （没有可见的微信主窗口 —— 先把微信登录好）")
    for i, w in enumerate(wins, 1):
        print(f"  [{i}] hwnd={w['hwnd']}  pid={w['pid']}  {w['w']}x{w['h']}")

    if len(wins) > 1 and dirs:
        print()
        print("账号 → 窗口（按「谁打开着这个账号的数据库」判断）：")
        for name in dirs:
            pid = account_window_pid(os.path.join(db_dir, name))
            hwnds = [w["hwnd"] for w in wins if w["pid"] == pid] if pid else []
            mark = f"hwnd={hwnds[0]}" if hwnds else "（查不到）"
            print(f"  {name} → pid={pid or '?'} {mark}")

    print()
    print("要锁定：把 wx_account 设成上面的账号目录名（或 wxid）；")
    print("窗口不听话时再把 wx_hwnd 设成对应 hwnd。")
    return 0


# ────────────────────────────── 语音自检 ──────────────────────────────


def voice_test(cfg: Config) -> int:
    """--voice-test：声卡 → 合成 → 播放 → 找语音键，四步各自报结果。

    前三步不需要微信；第四步要微信开着，找不到按钮时按提示跑 --probe-voice。
    """
    print("① 声卡")
    outs = voice_lib.list_outputs()
    for idx, name in outs:
        print("   [%d] %s" % (idx, name))
    device = voice_lib.find_output(cfg.voice_device)
    if device is None:
        print("   ✗ 没有「%s」：先装 VB-Cable（装完重新登录微信），"
              "或把 config.json 的 voice_device 改成上面某台" % cfg.voice_device)
        return 1
    print("   ✓ 播到 %s" % voice_lib.hw_output_name(device))

    print("② 合成（%s）" % cfg.voice_engine)
    out_dir = os.path.join(os.getcwd(), "_voice")
    try:
        wav, used = voice_lib.speak(
            "你好呀，我是小鲸鱼，测试一下声音。", out_dir, cfg.voice_engine,
            rate=cfg.voice_rate, pitch=cfg.voice_pitch, voice_name=cfg.voice_name,
            breeze_key=cfg.voice_breeze_key, breeze_voice=cfg.voice_breeze_voice)
    except voice_lib.VoiceError as exc:
        print("   ✗ %s" % exc)
        print("   中文语音包：设置 → 时间和语言 → 语音 → 添加「中文(简体)」")
        return 1
    print("   ✓ 引擎 %s：%s（%.0f KB）" % (used, wav, os.path.getsize(wav) / 1024))

    print("③ 播放（听得到/录音软件有电平就说明通了）")
    try:
        dur = voice_lib.play_wav(wav, device)
    except voice_lib.VoiceError as exc:
        print("   ✗ %s" % exc)
        return 1
    print("   ✓ %.1fs" % dur)

    print("④ 录语音的方式")
    if cfg.voice_key:
        vk = voice_lib.vk_for(cfg.voice_key)
        if vk is None:
            print("   ✗ 不认识的快捷键：%r（支持 alt_r / ctrl_r / shift_r 等）" % cfg.voice_key)
            return 1
        print("   ✓ 按住 %s 说话，松开发送（不用找按钮）" % cfg.voice_key)
        print("\n链路通了。把 config.json 的 voice_enabled 改成 true 就会发语音。")
        return 0

    try:
        bot = Bot(cfg, db=None, dry_run=True)
        wx = bot._ensure_wx()
        uia = wx._gui._get_uia()
        pos = bot._voice_button_pos(uia) if uia is not None else None
    except Exception as exc:
        print("   ✗ 拿不到微信窗口：%s（先把微信登录好再跑）" % exc)
        return 1
    if pos is None:
        print("   ✗ 认不出来。跑 .\\run.cmd --probe-voice 看控件列表；"
              "也可以先手工试：把鼠标放进输入框按住不动，"
              "会弹「松开发送 / 取消发送」的话，把 voice_button 填成 \"box\" 就行")
        return 1
    print("   ✓ (%d, %d)" % pos)
    print("\n链路通了。把 config.json 的 voice_enabled 改成 true 就会发语音。")
    return 0


def breeze_voices(cfg: Config) -> int:
    """--voice-breeze-voices：列出 BreezeBlue 里的中文女声（挑 voice_breeze_voice 用）。"""
    try:
        from breeze_blue import BreezeBlue
    except Exception as exc:
        print("没装 SDK：pip install breeze-blue（%r）" % (exc,))
        return 1
    key = (cfg.voice_breeze_key or os.environ.get("BREEZE_API_KEY", "")).strip()
    if not key:
        print('先配 key：config.json 的 "voice_breeze_key"，或 setx BREEZE_API_KEY "brz_..."')
        return 1
    try:
        client = BreezeBlue(api_key=key, timeout=30.0)
        res = client.voices.search(language_code="zh", gender=["female"])
        voices = (res or {}).get("voices") or []
    except Exception as exc:
        print("查音色失败：%r" % (exc,))
        return 1

    print("中文女声 %d 个：" % len(voices))
    for v in voices[:30]:
        print("  %-30s %s" % (v.get("voice_id"), v.get("name")))
    if voices:
        print('\n挑一个填进 config.json： "voice_breeze_voice": "%s"' % voices[0].get("voice_id"))
        print("填完跑： .\\run.cmd --voice-play  试听")
    return 0


def voice_play(cfg: Config, text: str = "") -> int:
    """--voice-play：合成一句直接播到扬声器，专门用来调音色（不发消息、不走 CABLE）。"""
    text = (text or "嗯？你怎么突然想起我啦，我在的呢，刚刚还想跟你说说话。").strip()
    out_dir = os.path.join(os.getcwd(), "_voice")
    t0 = time.time()
    try:
        wav, used = voice_lib.speak(
            text, out_dir, cfg.voice_engine,
            rate=cfg.voice_rate, pitch=cfg.voice_pitch, voice_name=cfg.voice_name,
            breeze_key=cfg.voice_breeze_key, breeze_voice=cfg.voice_breeze_voice)
    except voice_lib.VoiceError as exc:
        print("✗ 合成失败：%s" % exc)
        return 1
    print("引擎 %s｜语速 %+d%%｜音高 %+d%%｜音色 %s"
          % (used, cfg.voice_rate, cfg.voice_pitch, cfg.voice_name or "自动"))
    print("文件：%s（%.0f KB，合成用了 %.1fs）"
          % (wav, os.path.getsize(wav) / 1024, time.time() - t0))
    try:
        dur = voice_lib.play_wav(wav, None)     # None = 系统默认设备（扬声器）
    except voice_lib.VoiceError as exc:
        print("✗ 播放失败：%s" % exc)
        return 1
    print("✓ 播完 %.2fs（这次走扬声器，没经过 CABLE）" % dur)
    print("想调：改 config.json 的 voice_rate / voice_pitch（百分比，正数=更快更亮）")
    return 0


def voice_try(cfg: Config, text: str = "", target: str = "文件传输助手") -> int:
    """--voice-try：真发一条语音到「文件传输助手」，端到端验一遍（不打扰别人）。"""
    text = (text or "你好呀，我是小鲸鱼，这是一条测试语音。").strip()
    print("准备发一条测试语音到「%s」：%s" % (target, text))
    if cfg.voice_key:
        print("方式：按住 %s 说话 → 播到 %s → 松开发送" % (cfg.voice_key, cfg.voice_device))
    try:
        bot = Bot(cfg, db=None, dry_run=True)
        wx = bot._ensure_wx()
        resp = bot._send_voice(wx, text, target)
    except Exception as exc:
        print("✗ 发送异常：%r" % (exc,))
        return 1

    ok = getattr(resp, "is_success", True)
    print(("✓ " if ok else "✗ ") + str(resp))

    # 再拿数据库核一遍：真落了一条「语音」才算通（界面看不出来的假成功太多了）
    try:
        db, _info = open_wechat_db(cfg)
        rows = db.get_messages("filehelper", limit=3) or []
        types = [str(r.get("type")) for r in rows]
        print("文件传输助手最新消息类型：%s" % ("、".join(types) or "（空）"))
        if types[:1] == ["语音"]:
            print("✓ 数据库确认：最新一条就是语音消息")
            ok = True
    except Exception as exc:
        print("（核对数据库失败：%r）" % (exc,))

    if ok:
        print("\n去微信「文件传输助手」里点开听一下；没问题就把 voice_enabled 改成 true。")
    else:
        print("\n没成。先手工确认一次：微信窗口在前台时，你自己按住右 Alt 1 秒再松开，"
              "会不会出「松开发送」的录音条？把这句的答案告诉我。")
    return 0 if ok else 1


def probe_voice(cfg: Config) -> int:
    """--probe-voice：把输入框周围那排控件列出来（排 voice_button 用）。

    微信的工具栏不一定会给控件起名字，所以除了「名字带语音」的，还会把输入框
    下方那一整条（工具栏所在的高度带）里**所有有名字的控件**都打出来。
    """
    try:
        bot = Bot(cfg, db=None, dry_run=True)
        wx = bot._ensure_wx()
        uia = wx._gui._get_uia()
        win = None
        if uia is not None:
            try:
                uia.ensure_window()
            except Exception:
                pass
            win = getattr(uia, "_win", None)
    except Exception as exc:
        print("拿不到微信窗口：%s（先把微信登录好再跑）" % exc)
        return 1
    if win is None:
        print("UIA 不可用（窗口没起来或者锁屏了）")
        return 1

    box = None
    try:
        box = uia._chat_input()
    except Exception:
        pass
    br = None
    if box is not None:
        r = box.BoundingRectangle
        br = (r.left, r.top, r.right, r.bottom)
        print("输入框：(%d,%d)-(%d,%d)  Name=%r" % (br[0], br[1], br[2], br[3], box.Name))
    else:
        print("输入框：没找到（窗口没打开会话？）")

    named, voiceish, stack = [], [], [(win, 0)]
    while stack:
        ctrl, depth = stack.pop()
        if depth < 10:
            try:
                for child in ctrl.GetChildren():
                    stack.append((child, depth + 1))
            except Exception:
                pass
        try:
            name = (ctrl.Name or "").strip()
            rect = ctrl.BoundingRectangle
            ctype = ctrl.ControlTypeName
        except Exception:
            continue
        if not name or rect.width() <= 0 or rect.height() <= 0:
            continue
        row = (ctype, name, (rect.left, rect.top, rect.right, rect.bottom), depth)
        if any(k in name for k in ("语音", "说话", "麦克风", "录音", "按住", "长按")):
            voiceish.append(row)
        if br and br[3] - 40 <= rect.top <= br[3] + 200:
            named.append(row)

    print("\n== 名字像语音的控件（%d 个）==" % len(voiceish))
    for ctype, name, r, depth in sorted(voiceish, key=lambda h: (h[2][1], h[2][0])):
        print("  %-16s (%4d,%4d)-(%4d,%4d) d=%d %r"
              % (ctype, r[0], r[1], r[2], r[3], depth, name))

    print("\n== 输入框下面那一条里的控件（%d 个）==" % len(named))
    for ctype, name, r, depth in sorted(named, key=lambda h: (h[2][1], h[2][0]))[:40]:
        print("  %-16s (%4d,%4d)-(%4d,%4d) d=%d %r"
              % (ctype, r[0], r[1], r[2], r[3], depth, name))

    print("\n填法（config.json）：")
    print('  "voice_button": "box"          # 直接按住输入框（微信提示「长按鼠标 语音输入」）')
    print('  "voice_button": "win:623,975"  # 窗口左上角起算的坐标')
    print('  "voice_button": "1192,1110"    # 屏幕绝对坐标')
    return 0


# ────────────────────────────── 机器人 ──────────────────────────────


class Bot:
    def __init__(self, cfg: Config, db=None, dry_run: bool = False):
        self.cfg = cfg
        self.db = db
        self.dry_run = dry_run or cfg.dry_run
        # dry-run 一律用内存态记忆：否则录一次演示，假档案就永久留在 memory.json 里
        self.sessions = SessionStore(cfg, None if self.dry_run else (cfg.memory_file or None))
        self.limiter = RateLimiter(cfg.rate_limit)
        self.echo = EchoGuard()
        self.llm = DeepSeekClient(cfg)
        self.search = WebSearch(cfg)
        self._search_limiter = RateLimiter(cfg.web_search_per_hour)

        self._wx = None                 # wechatauto.WeChat，首次发送时才创建
        self._wx_hwnd = 0               # 实际使用的微信主窗口句柄
        self._wx_lock = threading.Lock()
        # 所有 UIA/键鼠动作走这一个线程（COM 对象与线程绑定，见 _sender_loop）
        self._task_q: queue.Queue = queue.Queue()
        self._sender: threading.Thread | None = None
        self._sender_lock = threading.Lock()
        self._apology_at: dict[str, float] = {}
        self._name_cache: dict[str, str] = {}       # chat_id -> 显示名
        self._ambiguous_checked: set[str] = set()
        self._unresolved: set[str] = set()          # 查不到名字的会话（只警告一次）
        self._sender_cache: dict[tuple, str] = {}   # (chat_id, wxid) -> 发送人名字
        self._group_member_cache: dict[str, tuple] = {}  # chat_id -> (时间, {wxid: 名字})
        self._kaomoji_at: dict[str, float] = {}     # chat_id -> 上次贴颜文字的时间
        # 主动说话（实验功能）：灰度名单、冷却、每小时配额
        self._proactive_limiter = RateLimiter(cfg.proactive_max_per_hour)
        self._last_proactive: dict[str, float] = {}  # chat_id -> 上次主动发言时间
        self._last_in: dict[str, float] = {}         # chat_id -> 上一条消息时间（算安静了多久）
        # 后台维护（会话摘要 / 每日反思）不能两条同时跑
        self._maint_lock = threading.Lock()
        # 识图：每个会话最近收到的图片，等后面那条 @/文字来认领
        self._recent_images: dict[str, list] = {}
        self._vision_limiter = RateLimiter(cfg.vision_max_per_hour)
        self._media = None
        self._img_lock = threading.Lock()    # 同一时间只取一张图（取图可能卡在等密钥）
        # 看门狗：哪条消息从「开始想」到现在还没发出去（排查「不回消息」）
        self._inflight: dict[str, float] = {}
        self._stuck_logged: dict[str, float] = {}
        self._active = 0                     # 正在回消息的条数，后台维护要避让
        self._last_msg_at = 0.0
        base = os.path.dirname(os.path.abspath(cfg.memory_file)) if cfg.memory_file else os.getcwd()
        self._img_dir = os.path.join(base, "_images")
        self._voice_dir = os.path.join(base, "_voice")   # 合成出来的 wav 临时放这儿
        self._voice_fails = 0        # 连续失败次数
        self._voice_off = False      # 连挂几次就本次运行不再试（免得每条都卡几秒）
        self._image_key = ""         # 图片 AES 密钥（启动时从磁盘/内存拿到）

    # -- 发送 --------------------------------------------------------------

    def _ensure_wx(self):
        if self._wx is not None:
            return self._wx
        with self._wx_lock:
            if self._wx is None:
                from wechatauto import WeChat
                from wechatauto.guia import WeChatGUI

                log.info("正在初始化微信窗口控制器…")
                hwnd = pick_send_window(self.cfg, self.db) if self.db is not None else 0
                gui = WeChatGUI(hwnd=hwnd) if hwnd else WeChatGUI()
                self._wx_hwnd = gui.main_hwnd
                # 微信收在托盘/最小化时 UIA 树是空的，先把窗口拉回来再钉 UIA
                if restore_window(gui.main_hwnd, gui):
                    try:
                        gui._update_render_rect()
                    except Exception:
                        pass
                # 复用同一个 WeChatDB 实例，省掉一次数据库解密
                wx = WeChat(gui=gui, db=self.db) if self.db is not None else WeChat(gui=gui)
                # 坐标和 UIA 必须是同一个窗口，否则多开时会「看着这个、发到那个」
                pin_uia_window(gui, gui.main_hwnd)
                self._wx = wx
                log.info("微信控制器就绪（账号：%s，窗口：hwnd=%s）",
                         self._wx.nickname, gui.main_hwnd)
        return self._wx

    def resolve_name(self, chat_id: str) -> str:
        """把 wxid / 群号换成界面上能搜到的显示名（备注优先）。

        微信搜索框不认 `23136276396@chatroom` 这种内部 id。直接拿它当关键词，
        每条消息都要先空搜一次再退回昵称——又慢又难看。这里提前查库换好名字，
        `send_msg` 第一步就能命中。
        """
        cached = self._name_cache.get(chat_id)
        if cached:
            return cached

        name = chat_id
        resolved = False
        try:
            if self.db is not None:
                got = self.db.get_nickname(chat_id) or ""
                if got and got != chat_id:
                    name = got
                    resolved = True
                    self._warn_if_ambiguous(name)
        except Exception as exc:
            log.warning("解析 %s 的显示名失败：%r（退回用 id 搜）", chat_id, exc)

        # 没解析出名字时**不缓存**：你去微信里给群设个备注，下一条消息就能用上，
        # 不用重启。只有真拿到名字才值得缓存。
        if not resolved:
            if chat_id not in self._unresolved:
                self._unresolved.add(chat_id)
                log.warning(
                    "会话 %s 在数据库里没有名字（多半是没打开过/未保存的群），"
                    "搜索框不认群号，发送可能失败。建议在微信里打开这个群、"
                    "并给它设一个备注。", chat_id)
            return name

        self._name_cache[chat_id] = name
        log.info("会话名：%s → %s", chat_id, name)
        return name

    def _warn_if_ambiguous(self, name: str) -> None:
        """重名时 open_chat 会选第一个匹配，提前提醒一句。"""
        if name in self._ambiguous_checked or self.db is None:
            return
        self._ambiguous_checked.add(name)
        try:
            hits = {h.get("username") for h in self.db.search_contact(name) if h}
        except Exception:
            return
        if len(hits) > 1:
            log.warning("「%s」匹配到 %d 个联系人，搜索可能选错人；建议给它们加不同的备注",
                        name, len(hits))

    def send(self, chat_id: str, text: str) -> bool:
        """发文本；多行会拆成几条发出去，[表情:xxx] 换成图片。"""
        # 兜底：模型偶尔会带回 HTML（线上真发过 <div style="text-align: center"></div>），
        # 这里再清一遍，避免绕过 on_message 的调用（主动插话、道歉、好感度）漏掉
        text = clean_reply(text)
        text, sticker_names = extract_stickers(text)
        # 兜底：调用方（比如主动插话）如果漏了抠标签，别把 [好感:+3] 这种发出去
        for extractor in (extract_notes, extract_moments, extract_affinity, extract_kaomoji):
            text = extractor(text)[0]
        # 模型自己发明的标签（[想念:...]、【心情：...】）也删掉，别发进聊天
        text, unknown = strip_unknown_tags(text)
        if unknown:
            log.warning("回复里有协议外的标签 %s，已删掉", "、".join(unknown))
        if self.cfg.reply_split:
            chunks = split_messages(text, TEXT_LIMIT, self.cfg.reply_split_max)
        else:
            chunks = split_text(text, TEXT_LIMIT)
        if not chunks and not sticker_names:
            return False

        who = self.resolve_name(chat_id)

        # 语音开关：这条要不要「说」出来。命中就发语音条、不再发文字；
        # 任何一步失败都会掉回下面的文字分支，不会把回复吞掉。
        speak_line = self._pick_voice_line(text)

        if self.dry_run:
            if speak_line:
                log.info("[dry-run] → %s: [语音] %s", who, speak_line)
            else:
                for c in chunks:
                    log.info("[dry-run] → %s: %s", who, c)
            for name in sticker_names[: self.cfg.sticker_max]:
                path = resolve_sticker(self.cfg.sticker_dir, name)
                log.info("[dry-run] → %s: [表情:%s] %s", who, name,
                         path or "（没找到这个文件）")
            return True

        voice_sent = False
        if speak_line:
            resp, err = self._submit("voice", speak_line, who)
            if err is not None:
                log.error("语音发送异常 %s：%r（改发文字）", who, err)
                self._note_voice_fail()
            elif getattr(resp, "is_success", None) is False:
                log.warning("语音没发出去 %s：%s（改发文字）", who, resp)
                self._note_voice_fail()
            else:
                voice_sent = True
                self._voice_fails = 0
                self.echo.remember(chat_id, speak_line)
                log.info("已发语音 %s：%s", who, speak_line[:40])

        ok = True
        if not voice_sent:
            for i, chunk in enumerate(chunks):
                if i:
                    time.sleep(1.2)   # 连续发送太快会被微信吞掉
                resp, err = self._submit("text", chunk, who)
                self.echo.remember(chat_id, chunk)
                if err is not None:
                    ok = False
                    log.error("发送异常 %s：%r", who, err)
                    self._name_cache.pop(chat_id, None)
                elif getattr(resp, "is_success", None) is False:
                    ok = False
                    log.error("发送失败 %s: %s", who, resp)
                    # 可能是群改名了，清掉缓存下次重新解析
                    self._name_cache.pop(chat_id, None)
                else:
                    log.info("已回复 %s（%d/%d）：%s", who, i + 1, len(chunks),
                             chunk[:40].replace("\n", " "))

        # 文字发完再补表情，模拟真人「先说一句，再甩张图」
        for name in sticker_names[: max(0, self.cfg.sticker_max)]:
            path = resolve_sticker(self.cfg.sticker_dir, name)
            if not path:
                log.warning("没有这个表情：%s（往 %s 里放一个 %s.gif 就能用）",
                            name, self.cfg.sticker_dir, name)
                continue
            time.sleep(1.2)
            resp, err = self._submit("image", path, who)
            if err is not None:
                log.error("表情发送异常 %s：%r", name, err)
            elif getattr(resp, "is_success", None) is False:
                log.error("表情发送失败 %s: %s", name, resp)
            else:
                log.info("已发表情 %s → %s", name, who)
        return ok

    # -- 专用发送线程 -------------------------------------------------------

    def _submit(self, kind: str, payload: str, who: str):
        """把一次发送交给发送线程，并同步等结果。

        kind: "text" 发文字 / "image" 发本地图片（表情包）/ "voice" 发语音条
        """
        box: dict = {"resp": None, "error": None, "event": threading.Event()}
        self._start_sender()
        self._task_q.put((kind, payload, who, box))
        # SendMsg 内部有 75s 总时限，这里留点余量
        if not box["event"].wait(timeout=180):
            return None, TimeoutError("发送线程 180s 无响应")
        return box["resp"], box["error"]

    def _start_sender(self) -> None:
        if self._sender and self._sender.is_alive():
            return
        with self._sender_lock:
            if self._sender and self._sender.is_alive():
                return
            self._sender = threading.Thread(
                target=self._sender_loop, name="wx-sender", daemon=True)
            self._sender.start()

    def _sender_loop(self) -> None:
        """所有 UIA / 键鼠操作都在这个线程里做。

        UIA 的 COM 对象是线程绑定的：`WeChat()` 在哪个线程创建，就最好一直在那个
        线程里用。监听器是「每个会话一个工作线程」，如果它们各自直接调 SendMsg，
        就会跨线程复用 COM 对象，偶发 `COMError(-2147220991,'事件无法调用任何订户')`
        ——日志里那 6 次就是这么来的。

        收拢到一个线程后这类错误基本消失；真失败时丢掉控制器重建再重试一次。
        """
        while True:
            item = self._task_q.get()
            if item is _SENTINEL:
                return
            kind, payload, who, box = item
            for attempt in (1, 2):
                try:
                    wx = self._ensure_wx()
                    # 每条发送前都看一眼：窗口可能在两次发送之间被最小化了，
                    # 而库里的 ensure_visible() 有 15 秒缓存，不会替我们还原
                    if restore_window(self._wx_hwnd or getattr(
                            getattr(wx, "_gui", None), "main_hwnd", 0),
                            getattr(wx, "_gui", None)):
                        try:
                            wx._gui._update_render_rect()
                        except Exception:
                            pass
                    if kind == "image":
                        box["resp"] = self._send_image(wx, payload, who)
                    elif kind == "voice":
                        box["resp"] = self._send_voice(wx, payload, who)
                    else:
                        box["resp"] = wx.SendMsg(payload, who=who)
                    box["error"] = None
                    break
                except Exception as exc:      # COMError / 超时 / 窗口被挡
                    box["error"] = exc
                    if attempt == 1:
                        log.warning("发送首次失败（%r），重建微信控制器后重试", exc)
                        # 丢掉可能已失效的 COM 对象，下次 _ensure_wx 会重建
                        self._wx = None
                        time.sleep(1.5)
                    else:
                        log.exception("发送两次都失败")
            box["event"].set()

    def _send_image(self, wx, path: str, who: str):
        """发一张本地图片（表情包）。

        库自带的 `send_file` 走的是「截图 + 坐标」找输入框，在这台机器上一直失败
        （日志：输入框探测连续失败），图片永远发不出去。而文字那条 UIA 路径是通的，
        所以这里改成用 UIA 直接操作输入框控件：SetFocus → Ctrl+V → 回车。
        uiautomation 的 SendKeys 会先 SetFocus，不依赖任何坐标。

        失败（或 UIA 不可用）时退回库的默认路径。
        """
        wait = float(getattr(self.cfg, "image_paste_wait", 3.0) or 3.0)
        try:
            from wechatauto import WxResponse
            from wechatauto.guia import WeChatGUI

            uia = wx._gui._get_uia()
            if uia is not None:
                # 上一条文字就是发到这个会话的，不要再搜一次群名。
                # 和 guia.send_msg 里的判断保持一致：当前会话已经是目标就跳过 open_chat。
                try:
                    already = (not who) or uia.current_chat() == who
                except Exception:
                    already = False
                if already or uia.open_chat(who):
                    elem = uia._chat_input()
                    if elem is not None and WeChatGUI.copy_files_to_clipboard([path]):
                        elem.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
                        elem.SendKeys("{Ctrl}v", waitTime=0.05)
                        time.sleep(wait)          # 等缩略图渲染出来再回车
                        elem.SendKeys("{Enter}", waitTime=0.05)
                        return WxResponse.success(f"图片已发送：{path}", data={"path": path})
                log.warning("UIA 发图：找不到会话或输入框，退回默认路径")
        except Exception as exc:
            log.warning("UIA 发图失败（%r），退回默认路径", exc)

        return wx.SendFiles(path, who=who)

    # -- 发语音 ------------------------------------------------------------

    # 语音键的名字。微信 4.x 的叫法可能是「按住说话」「语音消息」「语音」；
    # 带「输入/文字」的是语音转文字那颗，按下去会往输入框里写字，必须排除。
    _VOICE_BTN_WORDS = ("按住说话", "语音消息", "语音")
    _VOICE_BTN_SKIP = ("输入", "文字", "通话")

    def _pick_voice_line(self, text: str) -> str:
        """这条回复要不要发语音；要的话返回要念的那句话，否则返回空串。"""
        if self._voice_off or not should_voice(text, self.cfg):
            return ""
        return speakable(text, self.cfg.voice_max_chars)

    def _note_voice_fail(self) -> None:
        self._voice_fails += 1
        if self._voice_fails >= 3 and not self._voice_off:
            self._voice_off = True
            log.error("语音连着失败 %d 次，本次运行改成只发文字；先跑 "
                      "python wx_bot.py --voice-test 看是哪一步不通", self._voice_fails)

    def _win_rect(self, uia):
        try:
            r = uia._win.BoundingRectangle
            return (r.left, r.top, r.right, r.bottom)
        except Exception:
            return None

    def _box_hold_pos(self, uia):
        """按住输入框本身（微信提示「长按鼠标 语音输入」，录音就是从框里起的）。"""
        try:
            r = uia._chat_input().BoundingRectangle
        except Exception:
            return None
        # 靠上一点，避开底下那排工具栏图标
        return (r.left + r.right) // 2, r.top + max(12, min(40, (r.bottom - r.top) // 4))

    def _voice_button_pos(self, uia):
        """语音键的屏幕坐标；``voice_button`` 填了就直接用。"""
        raw = (self.cfg.voice_button or "").strip()
        if raw:
            low = raw.lower()
            if low in ("box", "input", "输入框"):
                return self._box_hold_pos(uia)
            try:
                if low.startswith("win:"):
                    win_rect = self._win_rect(uia)
                    if win_rect is None:
                        log.warning("voice_button 用了 win: 但拿不到窗口矩形")
                        return None
                    x, y = [int(v) for v in re.split(r"[,，\s]+", raw[4:].strip())[:2]]
                    return win_rect[0] + x, win_rect[1] + y
                x, y = [int(v) for v in re.split(r"[,，\s]+", raw)[:2]]
                return x, y
            except Exception:
                log.warning("voice_button 要写成 \"box\" / \"win:x,y\" / \"x,y\"，"
                            "现在读到 %r", raw)

        win = getattr(uia, "_win", None)
        if win is None:
            try:
                uia.ensure_window()
            except Exception:
                pass
            win = getattr(uia, "_win", None)
        if win is None:
            return None
        box_rect = None
        try:
            box = uia._chat_input()
            if box is not None:
                r = box.BoundingRectangle
                box_rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            box_rect = None

        best = None          # (分数, x, y, 名字)
        stack = [(win, 0)]
        while stack:
            ctrl, depth = stack.pop()
            if depth < 10:
                try:
                    for child in ctrl.GetChildren():
                        stack.append((child, depth + 1))
                except Exception:
                    pass
            try:
                name = (ctrl.Name or "").strip()
                rect = ctrl.BoundingRectangle
            except Exception:
                continue
            if not name or rect.width() <= 0 or rect.height() <= 0:
                continue
            if any(bad in name for bad in self._VOICE_BTN_SKIP):
                continue
            if not any(word in name for word in self._VOICE_BTN_WORDS):
                continue
            score = 1
            # 工具栏在输入框那一行附近；命中这块区域的优先
            if box_rect and box_rect[1] <= rect.top <= box_rect[3] + 160:
                score = 2
            if best is None or score > best[0]:
                best = (score, (rect.left + rect.right) // 2,
                        (rect.top + rect.bottom) // 2, name)
        if best is None:
            return None
        log.info("语音按钮：%r → (%d,%d)", best[3], best[1], best[2])
        return best[1], best[2]

    def _send_voice(self, wx, text: str, who: str):
        """合成一句话，按住微信的语音键把它「说」进麦克风。

        微信电脑版的语音条只从麦克风录，所以链路是:
            文字 --合成--> wav --waveOut--> CABLE Input
                                             └→ 微信从 CABLE Output 当麦克风收
        按住期间光标停在语音键上，松开才发送（中途移动鼠标会被微信当成取消）。
        """
        from wechatauto import WxResponse

        device = voice_lib.find_output(self.cfg.voice_device)
        if device is None:
            names = "、".join(n for _, n in voice_lib.list_outputs()) or "（一台都没找到）"
            return WxResponse.failure(
                "找不到声卡「%s」（装了 VB-Cable 才有）；现有：%s"
                % (self.cfg.voice_device, names))

        try:
            wav, used = voice_lib.speak(
                text, self._voice_dir, self.cfg.voice_engine,
                rate=self.cfg.voice_rate, pitch=self.cfg.voice_pitch,
                voice_name=self.cfg.voice_name,
                breeze_key=self.cfg.voice_breeze_key,
                breeze_voice=self.cfg.voice_breeze_voice)
        except voice_lib.VoiceError as exc:
            return WxResponse.failure("合成语音失败：%s" % exc)

        uia = None
        try:
            uia = wx._gui._get_uia()
        except Exception:
            uia = None
        if uia is None:
            return WxResponse.failure("UIA 不可用，发不了语音")

        try:
            already = (not who) or uia.current_chat() == who
        except Exception:
            already = False
        if not (already or uia.open_chat(who)):
            return WxResponse.failure("打不开会话：%s" % who)

        import ctypes

        user32 = ctypes.windll.user32
        key = voice_lib.vk_for(self.cfg.voice_key) if self.cfg.voice_key else None
        dur = 0.0
        if key is not None:
            # 快捷键是发给「当前前台窗口」的，微信不在前台时 Alt 会被别的程序吃掉
            try:
                if user32.GetForegroundWindow() != self._wx_hwnd:
                    wx._gui.bring_to_front(keep_topmost=False)
            except Exception:
                pass
            # 快捷键模式：按住右 Alt = 说话，松开 = 发送。不碰鼠标，不用找按钮。
            try:
                uia._chat_input().SetFocus()
            except Exception:
                pass
            time.sleep(0.15)
            voice_lib.key_down(key)
            try:
                time.sleep(0.5)                 # 等微信切进录音状态
                dur = voice_lib.play_wav(wav, device)
                time.sleep(0.35)                # 尾巴：把最后一个字录进去
            except voice_lib.VoiceError as exc:
                return WxResponse.failure("播放失败：%s" % exc)
            finally:
                voice_lib.key_up(key)           # 松开 = 发送
            how = self.cfg.voice_key
        else:
            pos = self._voice_button_pos(uia)
            if pos is None:
                return WxResponse.failure(
                    "既没配快捷键也认不出语音键：把 config.json 的 voice_key 设成 "
                    "alt_r，或者跑 python wx_bot.py --probe-voice 看控件再填 voice_button")
            x, y = pos
            uia._set_cursor(x, y)
            time.sleep(0.15)
            saved = uia._clear_transparent(x, y)   # 微信画在分层透明窗口上，不摘掉点不中
            down = False
            try:
                user32.mouse_event(0x0002, 0, 0, 0, 0)      # 左键按下 = 开始录
                down = True
                time.sleep(0.45)                            # 等微信切进录音状态
                dur = voice_lib.play_wav(wav, device)
                time.sleep(0.35)                            # 尾巴：把最后一个字录进去
                user32.mouse_event(0x0004, 0, 0, 0, 0)      # 松开 = 发送
                down = False
            except voice_lib.VoiceError as exc:
                return WxResponse.failure("播放失败：%s" % exc)
            finally:
                if down:
                    user32.mouse_event(0x0004, 0, 0, 0, 0)
                uia._restore_transparent(saved)
            how = "鼠标(%d,%d)" % (x, y)

        log.info("语音已发 %s（%.1fs，引擎 %s，方式 %s，声卡 %s）", who, dur, used,
                 how, voice_lib.hw_output_name(device))
        try:
            os.remove(wav)          # 发成功了就不用留；失败时留着好查
        except OSError:
            pass
        return WxResponse.success("语音已发送（%.1fs）" % dur, data={"path": wav})

    def _apologize(self, chat_id: str) -> None:
        """出错时最多 10 分钟安慰一次，避免 API 挂了就刷屏。"""
        now = time.time()
        if now - self._apology_at.get(chat_id, 0) < 600:
            return
        self._apology_at[chat_id] = now
        self.send(chat_id, "抱歉，我这边暂时出了点问题，等会儿再问我一次吧。")

    def _send_affinity(self, chat_id: str, person: str) -> None:
        """回一条准确的好感度：分数 + 档位 + 今天涨跌（不经过模型，模型会自己圆）。"""
        p = self.sessions.profile(person)
        score = int(p.get("affinity", AFFINITY_INIT))
        tier, _hint = tier_of(score)
        line = f"好感度 {score}/100（{tier}）"
        if p.get("affinity_day") == time.strftime("%Y-%m-%d"):
            up = int(p.get("affinity_up") or 0)
            down = int(p.get("affinity_down") or 0)
            bits = ([f"+{up}"] if up else []) + ([f"-{down}"] if down else [])
            if bits:
                line += "｜今天 " + "、".join(bits)
        log.info("好感度查询 %s → %d（%s）", person, score, tier)
        self.send(chat_id, line)

    # -- 收到消息 ----------------------------------------------------------

    def resolve_sender(self, chat_id: str, msg: dict) -> str:
        """把发送人的 wxid 换成界面上显示的名字。

        群聊里尤其重要：库里每条消息只带 sender_username（wxid_xxx），
        不在你通讯录的群友还得查群成员表才有名字。
        """
        wxid = str(msg.get("sender_username") or "")
        if not wxid:
            return ""
        key = (chat_id, wxid)
        cached = self._sender_cache.get(key)
        if cached is not None:
            return cached

        name = ""
        try:
            if self.db is not None:
                got = self.db.get_nickname(wxid) or ""
                if got and got != wxid:
                    name = got
                elif self.cfg.is_group(chat_id):
                    # 通讯录里没有的群友，去群成员表里找
                    name = self._group_names(chat_id).get(wxid, "")
        except Exception as exc:
            log.warning("解析发送人 %s 失败：%r", wxid, exc)

        self._sender_cache[key] = name
        return name

    def _group_names(self, chat_id: str) -> dict:
        """群成员 wxid → 显示名（备注优先），缓存 10 分钟。"""
        hit = self._group_member_cache.get(chat_id)
        if hit and time.time() - hit[0] < 600:
            return hit[1]

        mapping: dict[str, str] = {}
        try:
            for m in self.db.get_group_members(chat_id) or []:
                uid = m.get("username")
                if not uid:
                    continue
                mapping[uid] = m.get("remark") or m.get("nick_name") or uid
        except Exception as exc:
            log.warning("读取群成员失败 %s：%r", chat_id, exc)

        self._group_member_cache[chat_id] = (time.time(), mapping)
        return mapping

    def _profile_key(self, chat_id: str, msg: dict) -> str:
        """档案挂在「人」身上：群聊用发送人 wxid，私聊就是 chat_id 本身。"""
        return str(msg.get("sender_username") or "") or chat_id

    def _remember_who(self, chat_id: str, msg: dict, speaker: str) -> str:
        """长期记忆：把说话人的姓名/id 记进档案。返回档案 key。"""
        person = self._profile_key(chat_id, msg)
        name = speaker
        if not name and not self.cfg.is_group(chat_id):
            # 私聊时会话名就是人名；群聊里绝不能拿群名当人名
            name = self.resolve_name(chat_id)
        self.sessions.touch_profile(person, name=name, wxid=person)
        return person

    def is_developer(self, chat_id: str, msg: dict, speaker: str) -> bool:
        """这条消息是不是开发者本人发的。

        同时比对 wxid、解析出来的名字、会话 id（私聊时 chat_id 就是对方的 wxid），
        这样你填 wxid、昵称还是备注都能命中。
        """
        wanted = {str(x).strip().lower() for x in self.cfg.developer_ids if str(x).strip()}
        if not wanted:
            return False
        candidates = {
            str(msg.get("sender_username") or "").lower(),
            (speaker or "").lower(),
            chat_id.lower(),
        }
        candidates.discard("")
        return bool(candidates & wanted)

    # -- 识图 --------------------------------------------------------------

    def _remember_image(self, msg: dict) -> None:
        """图片消息本身不回复，先记下来，等后面那条 @/文字来认领。"""
        chat_id = str(msg.get("username") or "")
        if not chat_id:
            return
        items = self._recent_images.setdefault(chat_id, [])
        items.append({
            "sender": str(msg.get("sender_username") or chat_id),
            "local_id": msg.get("local_id"),
            "shard": msg.get("shard"),
            "ts": time.time(),
        })
        del items[:-5]                      # 每个会话只留最近 5 张
        log.info("收到图片 %s（来自 %s），等文字/@", chat_id, items[-1]["sender"])
    def _take_recent_image(self, chat_id: str, sender: str):
        """取这条发送人最近一张没过期的图，并把它从缓存里拿走（一图只用一次）。"""
        items = self._recent_images.get(chat_id) or []
        ttl = float(self.cfg.vision_image_ttl or 0)
        now = time.time()
        fresh = [i for i in items if now - i["ts"] <= ttl]
        picked = None
        for idx in range(len(fresh) - 1, -1, -1):     # 从新到旧找
            if fresh[idx]["sender"] == sender:
                picked = fresh.pop(idx)
                break
        self._recent_images[chat_id] = fresh
        return picked

    def _download_image(self, chat_id: str, rec: dict):
        """把图片从微信本地库里解出来，返回落盘路径；失败或超时返回 None。

        取图那一步可能在「等你点开大图、抓 AES 密钥」上卡最多 120 秒
        （wechatauto 的 monitor 逻辑）。卡住的正好是回复线程，表现就是
        「有人发了图之后就不说话了」。所以这里限时：超过 IMAGE_FETCH_WAIT
        这一轮就不看图，先回文字；后台那次取图继续跑，密钥抓到以后就快了。
        """
        if not self._img_lock.acquire(blocking=False):
            log.debug("上一张图还在取（多半在等密钥），这条先不看图")
            return None
        box: dict = {}
        done = threading.Event()

        def _work():
            try:
                from wechatauto import MediaDownloader

                if self._media is None:
                    os.makedirs(self._img_dir, exist_ok=True)
                    # 把密钥显式喂进去：进程每启动一次，cfg_dword 提取都可能失败，
                    # 存盘的那份能省掉一次「等你点开大图」的等待
                    self._media = MediaDownloader(
                        self.db, save_dir=self._img_dir,
                        image_key=self._image_key or None)
                box["path"] = self._media.download_image(
                    chat_id, rec.get("local_id"), tier="best", shard=rec.get("shard"))
                if box["path"] and not self._image_key:
                    # 慢路径（等你在微信里点开大图）终于抓到密钥了：存下来，下次直接用
                    try:
                        lib_key = self._media._load_persisted_key()
                    except Exception:
                        lib_key = None
                    if lib_key:
                        save_image_key(self._img_dir, lib_key)
                        self._image_key = lib_key
                        log.info("图片密钥已抓到并存盘，下次启动不用再等")
            except Exception as exc:
                box["error"] = exc
            finally:
                self._img_lock.release()
                done.set()

        threading.Thread(target=_work, name="wx-img", daemon=True).start()
        if not done.wait(IMAGE_FETCH_WAIT):
            log.warning("取图超过 %.0f 秒（多半在等图片密钥）%s/%s，这一轮先不看图；"
                        "在微信里点开一张大图看几秒，密钥抓到就好了",
                        IMAGE_FETCH_WAIT, chat_id, rec.get("local_id"))
            return None

        if box.get("error") is not None:
            log.warning("图片下载异常 %s/%s：%r", chat_id, rec.get("local_id"),
                        box["error"])
            return None
        path = box.get("path")

        if not path:
            log.warning(
                "图片取不出来 %s/%s（可能本机只有缩略图，或图片 AES 密钥还没抓到；"
                "在微信里点开一张大图看几秒，程序就能抓到密钥）",
                chat_id, rec.get("local_id"))
            return None

        # 微信 4.x 常把图片存成私有容器 .wxgf（HEVC），需要转成 jpg 才能发给模型
        if path.lower().endswith(".wxgf"):
            converted = self._convert_wxgf(path)
            if not converted:
                return None
            path = converted
        return path

    def _convert_wxgf(self, path: str):
        """把 .wxgf（HEVC 裸流）转成 jpg。

        库自带的转码走 `tempfile.TemporaryDirectory()`，在某些机器上会被安全软件/
        权限挡掉并**静默失败**，最后落一个 .wxgf 给我们——原样发给 DeepSeek 就是
        400 unsupported image。这里自己转，临时文件直接写在 _images/ 里。
        """
        try:
            import subprocess

            data = open(path, "rb").read()
            hevc = self._media._extract_hevc(data)
            if not hevc:
                log.warning("wxgf 里没找到 HEVC 数据：%s", os.path.basename(path))
                return None

            exe = shutil.which("ffmpeg")
            if not exe:
                try:
                    import imageio_ffmpeg
                    exe = imageio_ffmpeg.get_ffmpeg_exe()
                except Exception:
                    exe = None
            if not exe:
                log.warning("转码 .wxgf 需要 ffmpeg，但没找到（pip install imageio-ffmpeg）")
                return None

            os.makedirs(self._img_dir, exist_ok=True)
            stem = os.path.splitext(path)[0]
            src, dst = stem + ".hevc", stem + ".jpg"
            with open(src, "wb") as f:
                f.write(hevc)
            try:
                r = subprocess.run(
                    [exe, "-y", "-v", "error", "-i", src, "-frames:v", "1", dst],
                    capture_output=True, timeout=60)
            finally:
                try:
                    os.remove(src)
                except OSError:
                    pass

            if r.returncode != 0 or not os.path.exists(dst):
                log.warning("ffmpeg 转码失败（exit=%s）：%s",
                            r.returncode, (r.stderr or b"")[:200])
                return None
            with open(dst, "rb") as f:
                if f.read(3) != b"\xff\xd8\xff":
                    log.warning("转码结果不是 JPEG：%s", dst)
                    return None
            log.info("已把 .wxgf 转成 jpg：%s", os.path.basename(dst))
            return dst
        except Exception as exc:
            log.warning("转码 .wxgf 异常：%r", exc)
            return None

    def _grab_image(self, chat_id: str, person: str):
        """判断这条消息要不要配图：命中就下载并编码成 data URL。"""
        rec = self._take_recent_image(chat_id, person)
        if rec is None:
            return None
        if not self._vision_limiter.allow(person):
            log.warning("%s 触发识图限流（%s 张/小时），这次不看图",
                        person, self.cfg.vision_max_per_hour)
            return None

        path = self._download_image(chat_id, rec)
        if not path:
            return None
        try:
            url = encode_image(path, self.cfg.vision_max_side)
        except UnsupportedImage as exc:
            log.warning("这张图没法给模型看：%s", exc)
            return None
        except Exception as exc:
            log.warning("图片编码失败：%r", exc)
            return None
        log.info("识图 %s：%s（约 %d KB）",
                 person, os.path.basename(path), len(url) * 3 // 4 // 1024)
        return url

    def cleanup_images(self) -> int:
        """按保留策略清理 _images/，返回删掉的张数。"""
        return prune_images(self._img_dir,
                            keep_days=self.cfg.image_keep_days,
                            keep_max=self.cfg.image_keep_max)

    def cleanup_voice(self) -> int:
        """清掉 _voice/ 里发送失败留下的 wav，返回删掉的个数。"""
        return prune_voice(self._voice_dir)

    def setup_image_key(self) -> bool:
        """启动时准备好图片密钥（磁盘缓存优先，其次 cfg_dword 派生），返回是否可用。"""
        if self.db is None:
            return False
        try:
            self._image_key = ensure_image_key(self.db, self._img_dir)
        except Exception as exc:
            log.warning("准备图片密钥失败：%r", exc)
            self._image_key = ""
        return bool(self._image_key)

    def _run_tool(self, person: str, name: str, args_json: str) -> str:
        """模型点名的工具在这里执行。目前只有联网搜索。"""
        if name != "web_search":
            return f"（没有这个工具：{name}）"
        try:
            query = str((json.loads(args_json or "{}") or {}).get("query") or "").strip()
        except Exception:
            query = ""
        if not query:
            return "（缺少搜索关键词）"

        if not self._search_limiter.allow(person):
            log.warning("%s 触发搜索限流（%s 次/小时），这次不搜",
                        person, self.cfg.web_search_per_hour)
            return "（本小时搜索次数已用完，请直接按已有信息回答，或让对方稍后再问）"

        log.info("联网搜索 %s：%s", person, query)
        try:
            return self.search.search(query, self.cfg.web_search_count)
        except Exception as exc:
            log.warning("搜索失败：%r", exc)
            return f"（搜索失败：{exc}）"

    def _maybe_kaomoji(self, chat_id: str, text: str, mood: str) -> str:
        """模型只标情绪，贴不贴、贴哪个由这里决定：概率 + 同会话冷却。

        颜文字是调味，撒多了会腻。所以即使模型标了，也只按 kaomoji_rate 的概率贴，
        而且同一个会话 kaomoji_cooldown 秒内最多一次。
        """
        if not self.cfg.kaomoji_enabled or not mood:
            return text
        now = time.time()
        if now - self._kaomoji_at.get(chat_id, 0) < self.cfg.kaomoji_cooldown:
            return text
        if random.random() > self.cfg.kaomoji_rate:
            return text

        face = pick_kaomoji(mood)
        if not face:
            log.info("未知的颜文字情绪：%s（忽略）", mood)
            return text

        lines = (text or "").rstrip().split("\n")
        if not lines or not lines[-1].strip():
            return text
        lines[-1] = lines[-1].rstrip() + " " + face
        self._kaomoji_at[chat_id] = now
        log.info("贴颜文字 %s（%s）", face, mood)
        return "\n".join(lines)

    # ── 主动说话（实验功能，只在灰度名单里生效）────────────────────────

    def _gray_entries(self) -> set:
        return {str(x).strip().casefold()
                for x in (self.cfg.proactive_ids or []) if str(x).strip()}

    def _in_gray(self, chat_id: str, msg: dict) -> bool:
        """这个群/这个人是否在灰度名单里。

        名单里可以写群号、群名、对方昵称/备注、wxid，大小写不敏感。
        名单空 = 谁都不启用（这是故意的：宁可没效果，也别到处乱说话）。
        """
        entries = self._gray_entries()
        if not entries:
            return False
        if str(chat_id).casefold() in entries:
            return True
        sender = str(msg.get("sender_username") or "")
        if sender and sender.casefold() in entries:
            return True
        # 到这里才值得去查名字（有缓存，第一次查完就免费）
        name = self._name_cache.get(chat_id) or ""
        if name and name.casefold() in entries:
            return True
        speaker = self.resolve_sender(chat_id, msg)
        return bool(speaker and speaker.casefold() in entries)

    @staticmethod
    def _worth_considering(text: str) -> bool:
        """本地初筛：太短、纯表情、纯笑声、纯标点的消息不值得为它花一次 API。"""
        t = str(text or "").strip()
        if len(t) < 4:
            return False
        if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", t):
            return False
        if _NOISE.match(t):
            return False
        core = re.sub(r"[\s,，.。!！?？~～…、:：;；\"'“”‘’()（）]+", "", t)
        return len(core) >= 3

    def _maybe_proactive(self, chat_id: str, msg: dict, text: str,
                         prev_at: float | None) -> None:
        """没被 @ 时偶尔自己接一句。四道闸：初筛 → 灰度 → 冷却/配额 → 抽样。"""
        cfg = self.cfg
        if not self._worth_considering(text):
            return
        if not self._in_gray(chat_id, msg):
            return
        now = time.time()
        if now - self._last_proactive.get(chat_id, 0.0) < max(0, cfg.proactive_min_gap):
            return
        if not self._proactive_limiter.allow(chat_id):
            log.info("主动发言：%s 这一小时说得够多了", chat_id)
            return
        chance = max(0.0, min(1.0, float(cfg.proactive_chance)))
        if chance < 1.0 and random.random() > chance:
            return
        self._proactive_speak(chat_id, msg, text, prev_at)

    def _proactive_speak(self, chat_id: str, msg: dict, text: str,
                         prev_at: float | None) -> None:
        """让模型判断值不值得说，并直接生成一句短的；回 [不说] 就算了。"""
        speaker = self.resolve_sender(chat_id, msg)
        person = self._profile_key(chat_id, msg)
        self.sessions.touch_profile(person, name=speaker, wxid=person)
        try:
            messages = self.sessions.build(chat_id, text, speaker, False, person)
        except Exception as exc:
            log.warning("主动发言：构造提示词失败 %r", exc)
            return

        hint = ""
        if prev_at:
            gap = time.time() - prev_at
            if gap > 300:
                hint = f"\n这个群刚才安静了 {int(gap // 60)} 分钟，你想主动热一下气氛也可以。"
        messages[0]["content"] += PROACTIVE_NOTE + hint

        try:
            answer = self.llm.chat(messages)
        except DeepSeekError as exc:
            log.warning("主动发言：判断失败 %s", exc)
            return

        answer = (answer or "").strip()
        if not answer or "[不说]" in answer or "【不说】" in answer:
            log.info("主动发言：这条不接（%s）", chat_id)
            return

        # 主动插话只取正文：不记好感、不记瞬间、不发标签表情
        for extractor in (extract_stickers, extract_notes, extract_moments,
                          extract_affinity, extract_kaomoji):
            answer = extractor(answer)[0]
        answer = answer.strip()
        if not answer:
            log.info("主动发言：抠完标签什么都不剩，不发")
            return
        if len(answer) > 120:
            log.info("主动发言：太长（%d 字），不适合插话，不发", len(answer))
            return

        self._last_proactive[chat_id] = time.time()
        hist = format_user_content(self.cfg, chat_id, text, speaker)
        self.sessions.append(chat_id, hist, answer, person)
        log.info("主动插话 %s（%s）：%s", chat_id, speaker or "?",
                 answer[:40].replace("\n", " "))
        self.send(chat_id, answer)

    # ── 后台维护：会话摘要 + 每日反思 ──────────────────────────────

    def maintain(self) -> None:
        """定时维护，跑在后台线程里，不阻塞收发。"""
        # 正在回消息、或者刚聊完，就把这一轮让出去：摘要/反思都要调模型，
        # 跟聊天抢 API 只会让回复变慢（用户看到的就是「刚启动不回消息」）。
        if self._active:
            log.debug("跳过本轮维护：还有 %d 条回复在处理", self._active)
            return
        if self._last_msg_at and time.time() - self._last_msg_at < 90:
            log.debug("跳过本轮维护：%.0f 秒前还在聊天",
                      time.time() - self._last_msg_at)
            return
        if not self._maint_lock.acquire(blocking=False):
            return          # 上一轮还没跑完
        try:
            try:
                self._summarize_idle()
            except Exception as exc:     # 维护任务出错不能影响收发
                log.warning("会话摘要异常：%r", exc)
            try:
                self._reflect_daily()
            except Exception as exc:
                log.warning("记忆反思异常：%r", exc)
        finally:
            self._maint_lock.release()

    def watchdog(self) -> None:
        """有回复卡住超过 90 秒就在日志里点名。

        「刚启动不回消息」最难查的地方在于：卡住的时候日志什么都不写。
        这里把「开始想 → 发出去」这段时间记下来，超时打一条 warning，
        下次看日志就知道是卡在模型还是卡在发送。
        """
        now = time.time()
        for chat_id, since in list(self._inflight.items()):
            waited = now - since
            if waited < 90:
                continue
            if now - self._stuck_logged.get(chat_id, 0) < 120:
                continue        # 同一个会话 2 分钟内只提醒一次
            self._stuck_logged[chat_id] = now
            log.warning("%s 这条回复已经等了 %.0f 秒还没发出去"
                        "（卡在模型请求或微信发送上），继续等…", chat_id, waited)

    def _summarize_idle(self) -> None:
        """把闲置的会话压成 1~2 句摘要，存进那个人的档案。"""
        if not self.cfg.summary_enabled:
            return
        for item in self.sessions.take_idle_sessions(self.cfg.summary_after):
            dialogue = render_dialogue(item["msgs"])
            if len(dialogue) < 40:      # 就一句「在吗 / 在的」不值得压
                continue
            try:
                raw = self.llm.chat([
                    {"role": "system", "content": SUMMARY_SYSTEM},
                    {"role": "user", "content": dialogue},
                ])
            except DeepSeekError as exc:
                log.warning("会话摘要失败 %s：%s", item["person"], exc)
                continue
            summary = clean_summary(raw)
            if summary and self.sessions.add_summary(item["person"], summary):
                log.info("会话摘要 %s（%d 条消息）：%s",
                         item["person"], len(item["msgs"]), summary)

    def _reflect_daily(self) -> None:
        """每天一次：合并重复、消解矛盾、补上漏记的事实。"""
        if not self.cfg.reflect_enabled:
            return
        if not self.sessions.reflect_due(self.cfg.reflect_hours):
            return
        people = self.sessions.reflect_briefs(self.cfg.reflect_active_days)
        if not people:
            self.sessions.mark_reflected()
            return
        log.info("开始记忆反思：%d 个人", len(people))
        for person, brief in people:
            try:
                raw = self.llm.chat([
                    {"role": "system", "content": REFLECT_SYSTEM},
                    {"role": "user", "content": brief},
                ])
            except DeepSeekError as exc:
                log.warning("记忆反思失败 %s：%s", person, exc)
                continue
            stats = self.sessions.apply_reflection(person, parse_reflection(raw))
            if any(stats.values()):
                log.info("记忆反思 %s：替换 %d、删除 %d、新增 %d", person,
                         stats["replace"], stats["drop"], stats["add"])
        self.sessions.mark_reflected()

    def on_message(self, msg: dict, _listener=None) -> None:
        chat_id = str(msg.get("username") or "")
        self._last_msg_at = time.time()
        # 记一下上一条是什么时候：主动接话时用它判断「这个群冷清多久了」
        prev_at = self._last_in.get(chat_id)
        self._last_in[chat_id] = time.time()

        # 图片消息本身没有文字，先存起来；等后面那条 @/文字来了再一起看
        if msg.get("type") == IMAGE_TYPE:
            self._remember_image(msg)
            return

        d = decide(msg, self.cfg, getattr(self, "self_wxid", ""))
        if not d:
            # 没被 @ 的时候，主动说话功能有可能替她接一句（只在灰度名单里）
            if d.reason == "群里没 @ 我" and self.cfg.proactive_enabled:
                text = strip_sender_prefix(msg.get("content") or "").strip()
                self._maybe_proactive(chat_id, msg, text, prev_at)
                return
            if d.reason not in ("非文本消息(文本)", "群里没 @ 我"):
                log.info("跳过 %s：%s", chat_id, d.reason)
            return

        if self.echo.seen(chat_id, d.text):
            return

        if not self.limiter.allow(chat_id):
            log.warning("%s 触发限流（%s 次/小时），本条不回", chat_id, self.cfg.rate_limit)
            return

        # 「重置」这类命令
        if d.text.strip() in ("重置", "清空记忆", "新对话", "重新开始", "reset", "/reset"):
            self.sessions.reset(chat_id)
            self.send(chat_id, "好，之前的对话我忘掉了，我们重新开始 ✨")
            return

        # 「好感度」查询：直接给准确数字，不经过模型（模型报数会自己圆）
        if d.text.strip() in ("好感度", "好感查询", "/好感"):
            self._send_affinity(chat_id, self._profile_key(chat_id, msg))
            return

        started = time.time()
        # 从「开始处理」到「发出去」都算在处理中：看门狗据此发现卡住的回复
        # （识图下载、模型请求、微信发送都可能卡），后台维护据此避让。
        self._active += 1
        self._inflight[chat_id] = started
        try:
            speaker = self.resolve_sender(chat_id, msg)
            developer = self.is_developer(chat_id, msg, speaker)
            person = self._remember_who(chat_id, msg, speaker)   # 长期记忆：先落档
            image_url = self._grab_image(chat_id, person) if self.cfg.vision_enabled else None
            tools = [WEB_SEARCH_TOOL] if self.cfg.web_search_enabled else None
            runner = (lambda n, a: self._run_tool(person, n, a)) if tools else None
            try:
                messages = self.sessions.build(
                    chat_id, d.text, speaker, developer, person, image_url)
                # 带图这一轮换视觉模型；纯文字用默认模型
                answer = self.llm.chat(
                    messages,
                    model=self.cfg.vision_model if image_url else None,
                    tools=tools,
                    tool_runner=runner)
            except DeepSeekError as exc:
                log.error("DeepSeek 失败 %s：%s", chat_id, exc)
                self._apologize(chat_id)
                return
            except Exception as exc:
                log.exception("调用异常 %s：%r", chat_id, exc)
                self._apologize(chat_id)
                return

            # 模型偶尔会夹带 HTML。线上真发生过：整条只回一个
            # <div style="text-align: center"></div>，然后被原样发了出去。
            cleaned = clean_reply(answer)
            if cleaned != answer:
                log.info("回复里夹带了 HTML，已清理：%s",
                         (answer.strip()[:60] or "（整条都是标签）").replace("\n", " "))
                answer = cleaned

            # 模型可能在回复里夹带 [记住:xxx]，抠出来存进**说话人**的档案，别让它出现在聊天里
            answer, notes = extract_notes(answer)
            if notes:
                added = self.sessions.add_traits(person, notes)
                if added:
                    log.info("记住 %s 的印象 %d 条：%s", person, added, "、".join(notes))

            # [回忆:那一刻] → 记进「瞬间记忆」（是感受，不是事实）
            answer, moments = extract_moments(answer)
            for m in moments:
                if self.sessions.add_moment(person, m):
                    log.info("瞬间记忆 %s：%s", person, m)

            # [好感:+3 被夸] → 调分。单次/每日上限都在 bump_affinity 里卡死
            answer, affs = extract_affinity(answer)
            for delta, why in affs:
                score = self.sessions.bump_affinity(person, delta, why)
                tier, _ = tier_of(score)
                log.info("好感 %s %+d → %d（%s）%s",
                         person, delta, score, tier, ("·" + why) if why else "")
            # [颜:开心] → 按概率 + 冷却决定要不要真贴一个颜文字
            answer, mood = extract_kaomoji(answer)
            # 协议外的标签（模型自己发明的 [想念:...] 之类）不能当正文发出去
            answer, unknown = strip_unknown_tags(answer)
            if unknown:
                log.warning("模型发明了协议外的标签 %s，已删掉", "、".join(unknown))
            answer = self._maybe_kaomoji(chat_id, answer, mood)

            if not answer:
                log.info("这条回复没有可发的正文（只剩标签），不发送")
                return

            hist = format_user_content(self.cfg, chat_id, d.text, speaker)
            self.sessions.append(chat_id, hist + ("（附图）" if image_url else ""), answer, person)
            who_tag = "，".join(filter(None, [speaker, "开发者" if developer else ""]))
            log.info("回答 %s%s（%.1fs，%d 字）",
                     chat_id, f"（{who_tag}）" if who_tag else "",
                     time.time() - started, len(answer))
            self.send(chat_id, answer)
        finally:
            self._active -= 1
            self._inflight.pop(chat_id, None)


# ─────────────────────────── 自检 ───────────────────────────


def run_check(cfg: Config) -> int:
    ok = True
    print("1) 检查 DeepSeek …")
    try:
        reply = DeepSeekClient(cfg).chat(
            [{"role": "user", "content": "只回复两个字：收到"}]
        )
        print(f"   ✅ 通了，模型回复：{reply[:40]}")
    except Exception as exc:
        ok = False
        print(f"   ❌ 失败：{exc}")

    print("2) 检查微信数据库 …")
    try:
        db, info = open_wechat_db(cfg)      # 同样受 wx_account 约束
        print(f"   ✅ 账号：{info.get('nick_name')}（{info.get('username')}）")
        sessions = db.get_sessions(limit=10)
        print(f"   ✅ 读到 {len(sessions)} 个会话（示例）：")
        for s in sessions[:5]:
            print(f"      {s['username']:<28} 未读={s['unread']}  {(s['summary'] or '')[:20]}")
    except Exception as exc:
        ok = False
        print(f"   ❌ 失败：{exc!r}")
        print("      常见原因：微信没登录 / Python 不是 64 位 / 微信版本低于 4.1.12")
        print("      多开微信时用 --wx-list 看账号目录，填到 config.json 的 wx_account")

    print("3) 检查识图 …")
    if not cfg.vision_enabled:
        print("   已关闭（vision_enabled=false）")
    else:
        exe = shutil.which("ffmpeg")
        src = "PATH"
        if not exe:
            try:
                import imageio_ffmpeg
                exe, src = imageio_ffmpeg.get_ffmpeg_exe(), "imageio-ffmpeg"
            except Exception:
                exe = None
        if exe:
            print(f"   ✅ ffmpeg 可用（{src}）：{exe}")
        else:
            ok = False
            print("   ❌ 没有 ffmpeg —— 微信的 .wxgf 图片转不成 jpg，识图会失败")
            print("      pip install imageio-ffmpeg 即可")
        print(f"   模型：{cfg.vision_model}（detail={cfg.vision_detail}）")
        print("   提醒：图片是加密的，需在微信里点开一张大图看几秒让程序抓到密钥")

    print("4) 检查发送通道（会驱动微信窗口，请先别动鼠标）…")
    try:
        from wechatauto import WeChat

        wx = WeChat()
        print(f"   ✅ 控制器就绪，账号：{wx.nickname}")
        print("      想真的测一条，把下面这行取消注释：")
        print("      # wx.SendMsg('测试消息', who='文件传输助手')")
    except Exception as exc:
        ok = False
        print(f"   ❌ 失败：{exc!r}")

    print()
    print("全部通过 ✅" if ok else "有项目没通过 ❌")
    return 0 if ok else 1


# ─────────────────────────── 入口 ───────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek × 个人微信")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--check", action="store_true", help="自检后退出")
    parser.add_argument("--dry-run", action="store_true", help="只听不回")
    parser.add_argument("--chat", action="append", default=[], help="只监听指定会话（可多次）")
    parser.add_argument("--wx-list", action="store_true",
                        help="列出微信账号目录和窗口（多开时用来锁定）")
    parser.add_argument("--voice-test", action="store_true",
                        help="语音链路体检：声卡 → 合成 → 播放 → 录语音的方式")
    parser.add_argument("--voice-try", action="store_true",
                        help="真发一条测试语音到「文件传输助手」（端到端，安全）")
    parser.add_argument("--voice-play", action="store_true",
                        help="合成一句播到扬声器（调音色用，不发消息）")
    parser.add_argument("--voice-breeze-voices", action="store_true",
                        help="列出 BreezeBlue 的中文女声（挑音色 id 用）")
    parser.add_argument("--probe-voice", action="store_true",
                        help="列出微信窗口里带「语音」的控件（填 voice_button 用）")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"找不到配置文件 {args.config}")
        print("先复制一份： copy config.example.json config.json")
        return 1

    cfg = load_config(args.config)
    setup_logging(cfg)

    if args.wx_list:
        return wx_list(cfg)

    apply_rhythm(cfg)          # 拟人节流：默认关，否则每次动作要等 2.5~6 秒

    if args.voice_test:
        return voice_test(cfg)
    if args.voice_play:
        return voice_play(cfg)
    if args.voice_breeze_voices:
        return breeze_voices(cfg)
    if args.voice_try:
        return voice_try(cfg)
    if args.probe_voice:
        return probe_voice(cfg)

    # --check 即使没配 key 也要跑，好让人看到微信那两项的状态
    if not cfg.api_key and not args.check:
        print("还没有 DeepSeek API Key，二选一：")
        print('  1) 打开 config.json，把 "api_key": "" 填成 "api_key": "sk-..."')
        print('  2) 或者设环境变量： setx DEEPSEEK_API_KEY "sk-..."  （设完要重开终端）')
        print("申请地址： https://platform.deepseek.com/api_keys")
        return 1

    if args.check:
        return run_check(cfg)

    # 坑：wechatauto 根包导出的 Listener 是 wx.py 里的抽象基类（没有 __init__），
    # 真正的轮询监听器在 wechatauto.db 里。所以这两个都从 db 模块导。
    from wechatauto.db import Listener

    if cfg.wx_debug:
        from wechatauto import wxlog
        wxlog.set_debug(True)
        log.info("wechatauto 调试日志已打开（看 wechatauto_logs/）")

    patch_image_paste(cfg.image_paste_wait)

    # 同一个微信号只跑一个实例：两个实例会抢同一个窗口和同一份监听水位。
    # 放在开库之前，免得第二个实例先去重算一遍密钥再发现跑不了。
    mutex_key = (cfg.wx_account or "default").strip().lower()
    if not acquire_single_instance(mutex_key):
        log.error("同一个微信号已经有机器人在跑了，本次启动退出")
        print("\n已经有一个机器人进程在跑这个微信号了。")
        print("两个实例会抢同一个微信窗口（表现：消息时回时不回），先关掉那个再启动。")
        return 1

    # 打开数据前先打好日志：多开微信选错账号时，这一步会重算密钥、
    # 卡好几分钟，日志里要能看出来是卡在这儿。
    log.info("正在打开微信数据库%s…",
             f"（锁定账号 {cfg.wx_account}）" if cfg.wx_account else "")
    try:
        db, info = open_wechat_db(cfg)
    except Exception as exc:
        log.error("打开微信数据库失败：%s", exc)
        print(f"\n打开微信数据库失败：{exc}")
        print("可以先跑一下： run.cmd --wx-list  看本机有哪些账号目录")
        return 1

    bot = Bot(cfg, db=db, dry_run=args.dry_run)
    bot.self_wxid = info.get("username") or ""
    nick = info.get("nick_name") or bot.self_wxid
    if not cfg.my_names and nick:
        # 群里 @ 我 时会显示昵称，自动补进去；备注/群名片不同的话请手动加到 my_names
        cfg.my_names = [nick]

    log.info("登录账号：%s（%s）", nick, bot.self_wxid)
    log.info("模型：%s｜群聊策略：%s｜白名单：%s",
             cfg.model, cfg.group_mode, cfg.whitelist or "不限制")
    log.info("人设：%s（%d 字）",
             cfg.system_prompt_file or "config.json 里的 system_prompt",
             len(cfg.system_prompt))

    # 模型会自己发明标签：该写 [回忆:] 时写成 [想念:]，程序不认识就发进聊天了。
    # 明确告诉它只有这五个，别的不存在（程序侧还有 strip_unknown_tags 兜底）。
    cfg.system_prompt += (
        "\n\n## 标签协议（必须遵守）\n"
        "回复里只允许这五个标签：`[记住:内容]`、`[好感:N]`、`[回忆:一句话]`、"
        "`[颜:情绪]`、`[表情:名字]`。\n"
        "不许发明别的标签（比如 `[想念:]`、`[心情:]`、`[印象:]`）——"
        "程序不认识它们，会直接删掉。"
    )

    # 告诉模型有哪些表情可用，否则它只能瞎猜关键词
    stickers = list_stickers(cfg.sticker_dir)
    if stickers:
        log.info("可用表情 %d 个：%s", len(stickers), "、".join(stickers[:12]))
        cfg.system_prompt += (
            "\n\n## 可用表情\n"
            "想发表情时，在回复里另起一行写 [表情:名字]，程序会把 stickers/ 里同名的那张图"
            "作为一条独立的表情消息发出去。一条回复最多一张，宁可不发也别硬凑。\n"
            "可选：" + "、".join(stickers)
        )
    else:
        log.info("表情目录 %s 是空的，先不放表情", cfg.sticker_dir)

    if cfg.vision_enabled:
        log.info("识图：%s（detail=%s，%d 张/小时/人）",
                 cfg.vision_model, cfg.vision_detail, cfg.vision_max_per_hour)
        # 图片密钥：先看存过的那份，没有再赌一次内存派生；拿到就落盘
        if bot.setup_image_key():
            log.info("图片密钥：可用 ✅")
        else:
            log.info("图片密钥：没拿到 —— 识图会先等你在微信里点开一张大图"
                     "（最多 %d 秒），抓不到就只回文字", int(IMAGE_FETCH_WAIT))
    else:
        log.info("识图：已关闭")

    if bot.dry_run:
        log.info("dry-run 模式：只打印，不发送；记忆只留在内存，不会动 memory.json")
    if bot.dry_run or not cfg.memory_file:
        log.info("记忆：只存内存（重启就忘，设 memory_file 可持久化）")
    else:
        log.info("记忆：%s", cfg.memory_file)

    if cfg.log_file and int(cfg.log_max_bytes or 0) > 0:
        log.info("日志：%s（单文件 %d MB，留 %d 份）",
                 cfg.log_file,
                 max(1, int(cfg.log_max_bytes) // (1024 * 1024)), cfg.log_backups)

    # 识图落盘的图片只增不减会一直涨，启动先清一遍
    dropped = bot.cleanup_images()
    if dropped:
        log.info("清理识图临时图片 %d 张（保留 %d 天 / 最多 %d 张）",
                 dropped, cfg.image_keep_days, cfg.image_keep_max)
    # 发语音失败时留下的 wav 同理，启动清一遍
    stale = bot.cleanup_voice()
    if stale:
        log.info("清理语音临时文件 %d 个", stale)

    t_listen = time.time()
    listener = Listener(db, interval=1.0)
    if args.chat:
        for chat_id in args.chat:
            listener.add_listener(chat_id, bot.on_message)
            log.info("监听指定会话：%s", chat_id)
    else:
        listener.add_all(bot.on_message, discover=True)
        log.info("监听全部会话（自动发现新会话）：%d 个", len(listener.watermark))

    listener.start()
    log.info("已启动，Ctrl+C 退出（监听就绪 %.1fs）", time.time() - t_listen)

    if cfg.summary_enabled:
        log.info("会话摘要：闲置 %d 秒后压缩（每人留 %d 条）",
                 cfg.summary_after, cfg.summary_keep)
    if cfg.reflect_enabled:
        log.info("记忆反思：每 %d 小时一次", cfg.reflect_hours)
    log.info("后台维护：聊天时自动跳过，闲下来才跑摘要/反思")

    next_cleanup = time.time() + 3600       # 每小时清一次识图临时文件
    next_maint = time.time() + 60           # 启动 1 分钟后先跑一轮摘要/反思
    next_watch = time.time() + 15           # 看门狗：发现卡住的回复
    try:
        while True:
            time.sleep(1)
            if time.time() >= next_cleanup:
                next_cleanup = time.time() + 3600
                dropped = bot.cleanup_images()
                if dropped:
                    log.info("清理识图临时图片 %d 张", dropped)
                stale = bot.cleanup_voice()
                if stale:
                    log.info("清理语音临时文件 %d 个", stale)
            if time.time() >= next_watch:
                next_watch = time.time() + 15
                bot.watchdog()
            if time.time() >= next_maint:
                next_maint = time.time() + 300
                threading.Thread(target=bot.maintain, name="wx-maint",
                                 daemon=True).start()
    except KeyboardInterrupt:
        log.info("正在退出…")
    finally:
        listener.stop()
        bot.sessions.flush()      # 退出前把记忆写盘，别丢了最近的对话
        log.info("记忆已保存")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
