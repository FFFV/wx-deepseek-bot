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
    split_messages,
    split_text,
    strip_sender_prefix,
    tier_of,
)

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
        base = os.path.dirname(os.path.abspath(cfg.memory_file)) if cfg.memory_file else os.getcwd()
        self._img_dir = os.path.join(base, "_images")

    # -- 发送 --------------------------------------------------------------

    def _ensure_wx(self):
        if self._wx is not None:
            return self._wx
        with self._wx_lock:
            if self._wx is None:
                from wechatauto import WeChat
                log.info("正在初始化微信窗口控制器…")
                # 复用同一个 WeChatDB 实例，省掉一次数据库解密
                self._wx = WeChat(db=self.db) if self.db is not None else WeChat()
                log.info("微信控制器就绪（账号：%s）", self._wx.nickname)
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
        text, sticker_names = extract_stickers(text)
        # 兜底：调用方（比如主动插话）如果漏了抠标签，别把 [好感:+3] 这种发出去
        for extractor in (extract_notes, extract_moments, extract_affinity, extract_kaomoji):
            text = extractor(text)[0]
        if self.cfg.reply_split:
            chunks = split_messages(text, TEXT_LIMIT, self.cfg.reply_split_max)
        else:
            chunks = split_text(text, TEXT_LIMIT)
        if not chunks and not sticker_names:
            return False

        who = self.resolve_name(chat_id)

        if self.dry_run:
            for c in chunks:
                log.info("[dry-run] → %s: %s", who, c)
            for name in sticker_names[: self.cfg.sticker_max]:
                path = resolve_sticker(self.cfg.sticker_dir, name)
                log.info("[dry-run] → %s: [表情:%s] %s", who, name,
                         path or "（没找到这个文件）")
            return True

        ok = True
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

        kind: "text" 发文字 / "image" 发本地图片（表情包）
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
                    if kind == "image":
                        box["resp"] = self._send_image(wx, payload, who)
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
        """把图片从微信本地库里解出来，返回落盘路径；失败返回 None。"""
        try:
            from wechatauto import MediaDownloader

            if self._media is None:
                os.makedirs(self._img_dir, exist_ok=True)
                self._media = MediaDownloader(self.db, save_dir=self._img_dir)
            path = self._media.download_image(
                chat_id, rec.get("local_id"), tier="best", shard=rec.get("shard"))
        except Exception as exc:
            log.warning("图片下载异常 %s/%s：%r", chat_id, rec.get("local_id"), exc)
            return None

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

        speaker = self.resolve_sender(chat_id, msg)
        developer = self.is_developer(chat_id, msg, speaker)
        person = self._remember_who(chat_id, msg, speaker)   # 长期记忆：先落档
        image_url = self._grab_image(chat_id, person) if self.cfg.vision_enabled else None
        tools = [WEB_SEARCH_TOOL] if self.cfg.web_search_enabled else None
        runner = (lambda n, a: self._run_tool(person, n, a)) if tools else None
        started = time.time()
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
        answer = self._maybe_kaomoji(chat_id, answer, mood)

        if not answer:
            log.info("这条回复只有 [记住:...]，没有正文，不发送")
            return

        hist = format_user_content(self.cfg, chat_id, d.text, speaker)
        self.sessions.append(chat_id, hist + ("（附图）" if image_url else ""), answer, person)
        who_tag = "，".join(filter(None, [speaker, "开发者" if developer else ""]))
        log.info("回答 %s%s（%.1fs，%d 字）",
                 chat_id, f"（{who_tag}）" if who_tag else "",
                 time.time() - started, len(answer))
        self.send(chat_id, answer)


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
        from wechatauto.db import WeChatDB

        db = WeChatDB()
        info = db.get_self_info()
        print(f"   ✅ 账号：{info.get('nick_name')}（{info.get('username')}）")
        sessions = db.get_sessions(limit=10)
        print(f"   ✅ 读到 {len(sessions)} 个会话（示例）：")
        for s in sessions[:5]:
            print(f"      {s['username']:<28} 未读={s['unread']}  {(s['summary'] or '')[:20]}")
    except Exception as exc:
        ok = False
        print(f"   ❌ 失败：{exc!r}")
        print("      常见原因：微信没登录 / Python 不是 64 位 / 微信版本低于 4.1.12")

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
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"找不到配置文件 {args.config}")
        print("先复制一份： copy config.example.json config.json")
        return 1

    cfg = load_config(args.config)
    setup_logging(cfg)
    apply_rhythm(cfg)          # 拟人节流：默认关，否则每次动作要等 2.5~6 秒

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
    from wechatauto.db import Listener, WeChatDB

    if cfg.wx_debug:
        from wechatauto import wxlog
        wxlog.set_debug(True)
        log.info("wechatauto 调试日志已打开（看 wechatauto_logs/）")

    patch_image_paste(cfg.image_paste_wait)

    db = WeChatDB()
    info = db.get_self_info()
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
        keyfile = os.path.join(getattr(db, "workdir", "") or "", "image_keys.json")
        if keyfile and os.path.exists(keyfile):
            log.info("图片解密码：已缓存 ✅")
        else:
            log.info("图片解密码：还没抓到 —— 在微信里点开任意一张大图看几秒，程序会自己抓")
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

    listener = Listener(db, interval=1.0)
    if args.chat:
        for chat_id in args.chat:
            listener.add_listener(chat_id, bot.on_message)
            log.info("监听指定会话：%s", chat_id)
    else:
        listener.add_all(bot.on_message, discover=True)
        log.info("监听全部会话（自动发现新会话）")

    listener.start()
    log.info("已启动，Ctrl+C 退出")

    if cfg.summary_enabled:
        log.info("会话摘要：闲置 %d 秒后压缩（每人留 %d 条）",
                 cfg.summary_after, cfg.summary_keep)
    if cfg.reflect_enabled:
        log.info("记忆反思：每 %d 小时一次", cfg.reflect_hours)

    next_cleanup = time.time() + 3600       # 每小时清一次识图临时文件
    next_maint = time.time() + 60           # 启动 1 分钟后先跑一轮摘要/反思
    try:
        while True:
            time.sleep(1)
            if time.time() >= next_cleanup:
                next_cleanup = time.time() + 3600
                dropped = bot.cleanup_images()
                if dropped:
                    log.info("清理识图临时图片 %d 张", dropped)
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
