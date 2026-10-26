# -*- coding: utf-8 -*-
"""brain.py 的测试：不依赖微信、不依赖网络。

跑法：
    python test_brain.py
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import time
import unittest
import urllib.error
from unittest import mock

from brain import (
    TEXT_LIMIT,
    Config,
    DeepSeekClient,
    DeepSeekError,
    EchoGuard,
    RateLimiter,
    SessionStore,
    WEB_SEARCH_TOOL,
    WebSearch,
    config_from_dict,
    decide,
    is_mentioned,
    split_text,
    strip_mentions,
    strip_sender_prefix,
)

GROUP = "12345678901@chatroom"
FRIEND = "wxid_alice"


def msg(text="你好", mtype="文本", chat=FRIEND, sender="wxid_bob", **kw):
    d = {"type": mtype, "content": text, "username": chat, "sender_username": sender,
         "create_time": time.time(), "sort_seq": 1}
    d.update(kw)
    return d


def texts(items):
    """traits 现在存成 {t,w,hits,used,text}，测试里多半只关心文字。"""
    return [i["text"] if isinstance(i, dict) else i for i in (items or [])]


class TestCleaners(unittest.TestCase):
    def test_strip_sender_prefix(self):
        self.assertEqual(strip_sender_prefix("wxid_abc123:\n你好"), "你好")
        self.assertEqual(strip_sender_prefix("wxid_abc123:\n@小明 你好"), "@小明 你好")
        # 不是 wxid 开头的「xx:」不要剥，避免误伤正文
        self.assertEqual(strip_sender_prefix("2024:\n年度总结"), "2024:\n年度总结")

    def test_strip_sender_prefix_keeps_plain_colon(self):
        # 正文里的冒号不能被当成发送者前缀
        self.assertEqual(strip_sender_prefix("价格是：100 元"), "价格是：100 元")

    def test_mention_detection(self):
        self.assertTrue(is_mentioned("@小明 你好", ["小明"]))
        self.assertTrue(is_mentioned("@小明\u2005你好", ["小明"]))
        self.assertFalse(is_mentioned("@小明明 你好", ["小明"]), "不能把 @小明明 当成 @小明")
        self.assertFalse(is_mentioned("大家好", ["小明"]))

    def test_strip_mentions(self):
        self.assertEqual(strip_mentions("@小明 帮我写首诗", ["小明"]), "帮我写首诗")
        self.assertEqual(strip_mentions("@小明\u2005写首诗", ["小明"]), "写首诗")


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(my_names=["小明"])

    def test_private_text_ok(self):
        d = decide(msg("你好"), self.cfg)
        self.assertTrue(d.reply)
        self.assertEqual(d.text, "你好")

    def test_non_text_skipped(self):
        self.assertFalse(decide(msg("", mtype="图片"), self.cfg).reply)
        self.assertFalse(decide(msg("x", mtype="语音"), self.cfg).reply)

    def test_self_message_skipped(self):
        d = decide(msg("我自己发的", sender="wxid_me"), self.cfg, self_wxid="wxid_me")
        self.assertFalse(d.reply)
        self.assertEqual(d.reason, "自己发的")

    def test_group_requires_mention(self):
        self.assertFalse(decide(msg("大家好", chat=GROUP), self.cfg).reply)
        d = decide(msg("wxid_bob:\n@小明 你好", chat=GROUP), self.cfg)
        self.assertTrue(d.reply)
        self.assertEqual(d.text, "你好")

    def test_group_mode_all(self):
        cfg = Config(group_mode="all")
        self.assertTrue(decide(msg("大家好", chat=GROUP), cfg).reply)

    def test_group_mode_off(self):
        cfg = Config(group_mode="off", my_names=["小明"])
        self.assertFalse(decide(msg("wxid_bob:\n@小明 你好", chat=GROUP), cfg).reply)

    def test_whitelist(self):
        cfg = Config(whitelist=["wxid_stranger"])
        self.assertFalse(decide(msg("你好"), cfg).reply)
        self.assertTrue(decide(msg("你好", chat="wxid_stranger"), cfg).reply)

    def test_blacklist(self):
        cfg = Config(blacklist=[FRIEND])
        self.assertFalse(decide(msg("你好"), cfg).reply)

    def test_only_mention_no_body(self):
        d = decide(msg("wxid_bob:\n@小明", chat=GROUP), self.cfg)
        self.assertFalse(d.reply)


class TestSessionStore(unittest.TestCase):
    def test_history_and_truncation(self):
        cfg = Config(history_turns=2)
        st = SessionStore(cfg)
        for i in range(5):
            st.append(FRIEND, f"q{i}", f"a{i}")
        msgs = st.build(FRIEND, "new")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[-1], {"role": "user", "content": "new"})
        # 2 轮 = 4 条
        self.assertEqual(len(msgs), 1 + 4 + 1)
        self.assertEqual(msgs[1]["content"], "q3")

    def test_ttl_expiry(self):
        cfg = Config(session_ttl=1)
        st = SessionStore(cfg)
        st.append(FRIEND, "q", "a")
        st._data[FRIEND]["ts"] = time.time() - 5
        msgs = st.build(FRIEND, "new")
        # 对话历史被清掉了，只剩本轮
        self.assertEqual([m for m in msgs if m["role"] == "user"], [{"role": "user", "content": "new"}])
        # 但长期档案还在，用「上次聊到」把上下文接上
        self.assertIn("上次聊到", msgs[0]["content"])

    def test_reset(self):
        st = SessionStore(Config())
        st.append(FRIEND, "q", "a")
        self.assertTrue(st.reset(FRIEND))
        self.assertFalse(st.reset(FRIEND))


class TestRateLimiter(unittest.TestCase):
    def test_limit(self):
        rl = RateLimiter(limit=2)
        self.assertTrue(rl.allow("a"))
        self.assertTrue(rl.allow("a"))
        self.assertFalse(rl.allow("a"))
        self.assertTrue(rl.allow("b"), "不同会话独立计数")

    def test_window_rolls(self):
        rl = RateLimiter(limit=1, window=0.2)
        self.assertTrue(rl.allow("a"))
        self.assertFalse(rl.allow("a"))
        time.sleep(0.25)
        self.assertTrue(rl.allow("a"))


class TestEchoGuard(unittest.TestCase):
    def test_echo(self):
        g = EchoGuard()
        g.remember(FRIEND, "答案")
        self.assertTrue(g.seen(FRIEND, "答案"))
        self.assertFalse(g.seen(FRIEND, "别的"))
        self.assertFalse(g.seen("wxid_other", "答案"))


class TestSplitText(unittest.TestCase):
    def test_short(self):
        self.assertEqual(split_text("你好"), ["你好"])
        self.assertEqual(split_text(""), [])

    def test_long_splits_on_paragraph(self):
        text = "\n".join(["段落内容" * 20] * 20)   # 1600 字
        chunks = split_text(text, limit=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 300 for c in chunks), [len(c) for c in chunks])
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_no_boundary_hard_cut(self):
        chunks = split_text("啊" * 100, limit=30)
        self.assertEqual([len(c) for c in chunks], [30, 30, 30, 10])


class TestSplitMessages(unittest.TestCase):
    """多行回复拆成几条发：像真人一句一句打字。"""

    def test_multiline_becomes_bubbles(self):
        from brain import split_messages
        self.assertEqual(split_messages("好呀\n那你早点睡"), ["好呀", "那你早点睡"])

    def test_blank_lines_dropped(self):
        from brain import split_messages
        self.assertEqual(split_messages("第一句\n\n第二句\n   \n"), ["第一句", "第二句"])

    def test_single_line_stays_one(self):
        from brain import split_messages
        self.assertEqual(split_messages("就一句"), ["就一句"])

    def test_long_line_still_chunked(self):
        from brain import split_messages
        parts = split_messages("啊" * 100, limit=30)
        self.assertEqual([len(p) for p in parts], [30, 30, 30, 10])

    def test_cap_merges_tail(self):
        from brain import split_messages
        text = "\n".join(f"第{i}句" for i in range(8))
        parts = split_messages(text, max_parts=3)
        self.assertEqual(len(parts), 3, "不能一次刷 8 条")
        self.assertEqual(parts[-1], "\n".join(f"第{i}句" for i in range(2, 8)))

    def test_empty(self):
        from brain import split_messages
        self.assertEqual(split_messages(""), [])
        self.assertEqual(split_messages("  \n\n  "), [])


class TestSummaries(unittest.TestCase):
    """会话摘要：闲着的那段聊天压成 1~2 句，下次开场带回来。"""

    def test_add_and_inject(self):
        st = SessionStore(Config())
        st.touch_profile(FRIEND, name="阿鱼")
        self.assertTrue(st.add_summary(FRIEND, "聊了他换工作的事，有点焦虑"))
        text = st.profile_text(FRIEND)
        self.assertIn("之前聊过", text)
        self.assertIn("聊了他换工作的事，有点焦虑", text)

    def test_inject_limit(self):
        st = SessionStore(Config(summary_inject=1))
        st.touch_profile(FRIEND, name="阿鱼")
        st.add_summary(FRIEND, "第一次的摘要")
        st.add_summary(FRIEND, "第二次的摘要")
        text = st.profile_text(FRIEND)
        self.assertIn("第二次的摘要", text)
        self.assertNotIn("第一次的摘要", text, "只带最近的几条，控制 token")

    def test_deduped_and_capped(self):
        st = SessionStore(Config(summary_keep=3))
        st.touch_profile(FRIEND)
        self.assertTrue(st.add_summary(FRIEND, "同一句话"))
        self.assertFalse(st.add_summary(FRIEND, "同一句话"), "重复的不记")
        for i in range(5):
            st.add_summary(FRIEND, f"第{i}次")
        texts = [s["text"] for s in st.profile(FRIEND)["summaries"]]
        self.assertEqual(len(texts), 3)
        self.assertEqual(texts[-1], "第4次")

    def test_clean_summary(self):
        from brain import clean_summary
        self.assertEqual(clean_summary("「他最近在准备面试」"), "他最近在准备面试")
        self.assertEqual(clean_summary("无"), "")
        self.assertEqual(clean_summary("  \n "), "")
        self.assertLessEqual(len(clean_summary("啊" * 300)), 90)

    def test_take_idle_only(self):
        st = SessionStore(Config())
        st.append("c1", "q1", "a1", person="wxid_a")
        st.append("c2", "q2", "a2", person="wxid_b")
        st._data["c1"]["ts"] = time.time() - 1000
        taken = st.take_idle_sessions(600)
        self.assertEqual([t["chat_id"] for t in taken], ["c1"])
        self.assertEqual(taken[0]["person"], "wxid_a")
        self.assertEqual(st.messages("c1"), [], "取走之后会话要清掉")
        self.assertEqual(len(st.messages("c2")), 2, "还活跃的不能动")

    def test_take_idle_ignores_empty(self):
        st = SessionStore(Config())
        st._data["c1"] = {"msgs": [], "ts": time.time() - 1000}
        self.assertEqual(st.take_idle_sessions(600), [])


class TestReflection(unittest.TestCase):
    """每日反思：合并重复、消解矛盾。"""

    def test_replace_resolves_conflict(self):
        st = SessionStore(Config())
        st.touch_profile("wxid_a", name="张三")
        st.add_traits("wxid_a", ["在武汉读研", "养了只三花猫"])
        stats = st.apply_reflection("wxid_a", {
            "replace": [{"old": "在武汉读研", "new": "已毕业，在杭州工作"}],
        })
        self.assertEqual(stats, {"replace": 1, "drop": 0, "add": 0})
        texts = [t["text"] for t in st.profile("wxid_a")["traits"]]
        self.assertIn("已毕业，在杭州工作", texts)
        self.assertNotIn("在武汉读研", texts, "旧事实要被覆盖，不能两条并存")

    def test_add_and_drop(self):
        st = SessionStore(Config())
        st.touch_profile("wxid_a")
        st.add_traits("wxid_a", ["养了只三花猫"])
        stats = st.apply_reflection("wxid_a", {
            "add": ["最近在准备面试"], "drop": ["养了只三花猫"]})
        self.assertEqual(stats, {"replace": 0, "drop": 1, "add": 1})
        texts = [t["text"] for t in st.profile("wxid_a")["traits"]]
        self.assertEqual(texts, ["最近在准备面试"])

    def test_unknown_old_is_ignored(self):
        st = SessionStore(Config())
        st.touch_profile("wxid_a")
        st.add_traits("wxid_a", ["对花生过敏"])
        stats = st.apply_reflection("wxid_a", {
            "replace": [{"old": "完全不存在的印象", "new": "新事实"}]})
        self.assertEqual(stats["replace"], 0)
        self.assertEqual([t["text"] for t in st.profile("wxid_a")["traits"]],
                         ["对花生过敏"], "找不到就不动，别瞎加")

    def test_add_is_deduped(self):
        st = SessionStore(Config())
        st.touch_profile("wxid_a")
        st.add_traits("wxid_a", ["对花生过敏"])
        stats = st.apply_reflection("wxid_a", {"add": ["对花生过敏"]})
        self.assertEqual(stats["add"], 0)

    def test_parse_reflection(self):
        from brain import parse_reflection
        self.assertEqual(parse_reflection("{}"), {})
        self.assertEqual(parse_reflection("我觉得都挺好"), {})
        self.assertEqual(parse_reflection('[1, 2, 3]'), {})
        fenced = '```json\n{"drop": ["旧事"]}\n```'
        self.assertEqual(parse_reflection(fenced), {"drop": ["旧事"]})

    def test_due_and_mark(self):
        st = SessionStore(Config())
        self.assertTrue(st.reflect_due(24), "从没反思过，应该到期")
        st.mark_reflected()
        self.assertFalse(st.reflect_due(24))
        self.assertFalse(st.reflect_due(0), "最少也要隔 1 小时，防止反复调用")

    def test_briefs_only_active_people(self):
        st = SessionStore(Config())
        st.touch_profile("old", name="旧人")
        st.add_traits("old", ["很久以前的事"])
        st._profiles["old"]["last_seen"] = time.time() - 30 * 86400
        st.touch_profile("new", name="新人")
        st.add_traits("new", ["最近的事"])
        people = st.reflect_briefs(7)
        self.assertEqual([p for p, _ in people], ["new"])
        self.assertIn("现有印象", people[0][1])
        self.assertIn("最近的事", people[0][1])

    def test_brief_skips_profiles_without_memory(self):
        st = SessionStore(Config())
        st.touch_profile("empty", name="空档案")
        self.assertEqual(st.reflect_briefs(7), [])


class TestMemoryMaintenance(unittest.TestCase):
    """Bot 侧的调度：摘要和反思都走后台维护，失败不能影响收发。"""

    def _bot(self, reply, **kw):
        from wx_bot import Bot
        cfg = Config(api_key="sk-test", **kw)
        bot = Bot(cfg, db=None, dry_run=True)
        calls = []

        def fake(url, payload, headers, timeout):
            calls.append(payload["messages"][-1]["content"])
            return {"choices": [{"message": {"content": reply}}]}

        bot.llm = DeepSeekClient(cfg, transport=fake)
        bot.send = lambda *a, **k: True
        return bot, calls

    def test_summarize_idle_session(self):
        bot, calls = self._bot("他最近在准备面试，压力有点大")
        bot.sessions.append(
            "wxid_a", "张三：我下周要去面试了，有点紧张，怕过不了",
            "别怕，你准备得挺好的，面试前早点睡", person="wxid_a")
        bot.sessions._data["wxid_a"]["ts"] = time.time() - 1000
        bot._summarize_idle()
        self.assertEqual(len(calls), 1)
        sums = bot.sessions.profile("wxid_a")["summaries"]
        self.assertEqual(sums[-1]["text"], "他最近在准备面试，压力有点大")
        self.assertEqual(bot.sessions.messages("wxid_a"), [], "压完就清掉那段历史")

    def test_summary_none_is_skipped(self):
        bot, calls = self._bot("无")
        bot.sessions.append(
            "wxid_a", "张三：今天天气不错，我出门走走看看风景",
            "好呀，多晒晒太阳", person="wxid_a")
        bot.sessions._data["wxid_a"]["ts"] = time.time() - 1000
        self.assertEqual(len(bot.sessions.profile("wxid_a")["summaries"]), 0)
        bot._summarize_idle()
        self.assertEqual(len(bot.sessions.profile("wxid_a")["summaries"]), 0)

    def test_short_session_not_summarized(self):
        bot, calls = self._bot("随便")
        bot.sessions.append("wxid_a", "在吗", "在的", person="wxid_a")
        bot.sessions._data["wxid_a"]["ts"] = time.time() - 1000
        bot._summarize_idle()
        self.assertEqual(calls, [], "就一句话不值得花一次 API")

    def test_daily_reflect_updates_traits(self):
        bot, calls = self._bot(
            '{"replace": [{"old": "在武汉读研", "new": "已毕业，在杭州工作"}],'
            ' "add": ["最近在准备面试"]}')
        bot.sessions.touch_profile("wxid_a", name="张三")
        bot.sessions.add_traits("wxid_a", ["在武汉读研"])
        bot._reflect_daily()
        texts = [t["text"] for t in bot.sessions.profile("wxid_a")["traits"]]
        self.assertIn("已毕业，在杭州工作", texts)
        self.assertIn("最近在准备面试", texts)
        self.assertEqual(len(calls), 1)
        bot._reflect_daily()
        self.assertEqual(len(calls), 1, "24 小时内不再跑")

    def test_bad_json_keeps_memory_intact(self):
        bot, calls = self._bot("我觉得都挺好，不用改")
        bot.sessions.touch_profile("wxid_a", name="张三")
        bot.sessions.add_traits("wxid_a", ["对花生过敏"])
        bot._reflect_daily()
        self.assertEqual([t["text"] for t in bot.sessions.profile("wxid_a")["traits"]],
                         ["对花生过敏"])
        self.assertEqual(len(calls), 1)

    def test_disabled_does_nothing(self):
        bot, calls = self._bot("随便", summary_enabled=False, reflect_enabled=False)
        bot.sessions.append(
            "wxid_a", "张三：我下周要去面试了，有点紧张，怕过不了",
            "别怕，你准备得挺好的，面试前早点睡", person="wxid_a")
        bot.sessions._data["wxid_a"]["ts"] = time.time() - 1000
        bot.maintain()
        self.assertEqual(calls, [])


class TestDeepSeek(unittest.TestCase):
    def _client(self, response):
        calls = {}

        def transport(url, payload, headers, timeout):
            calls["url"] = url
            calls["payload"] = payload
            calls["headers"] = headers
            if isinstance(response, Exception):
                raise response
            return response

        return DeepSeekClient(Config(api_key="sk-test"), transport=transport), calls

    def test_success(self):
        client, calls = self._client(
            {"choices": [{"message": {"content": "  你好呀  "}}]}
        )
        self.assertEqual(client.chat([{"role": "user", "content": "hi"}]), "你好呀")
        self.assertEqual(calls["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(calls["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(calls["payload"]["model"], "deepseek-flash")
        self.assertIs(calls["payload"]["stream"], False)

    def test_reasoner_has_no_temperature(self):
        client, calls = self._client({"choices": [{"message": {"content": "ok"}}]})
        client.cfg.model = "deepseek-reasoner"
        client.chat([{"role": "user", "content": "hi"}])
        self.assertNotIn("temperature", calls["payload"])

    def test_empty_content(self):
        client, _ = self._client({"choices": [{"message": {"content": "   "}}]})
        with self.assertRaises(DeepSeekError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_bad_structure(self):
        client, _ = self._client({"oops": 1})
        with self.assertRaises(DeepSeekError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_transport_error_propagates(self):
        client, _ = self._client(DeepSeekError("boom", 500))
        with self.assertRaises(DeepSeekError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_no_api_key(self):
        client = DeepSeekClient(Config(api_key=""))
        with self.assertRaises(DeepSeekError):
            client.chat([{"role": "user", "content": "hi"}])


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = config_from_dict({}, env={})
        self.assertEqual(cfg.model, "deepseek-flash")
        self.assertFalse(cfg.thinking, "聊天默认关思考")
        self.assertEqual(cfg.group_mode, "at")

    def test_env_overrides_file(self):
        cfg = config_from_dict({"api_key": "from-file", "model": "m1"},
                               env={"DEEPSEEK_API_KEY": "from-env"})
        self.assertEqual(cfg.api_key, "from-env")
        self.assertEqual(cfg.model, "m1")

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            config_from_dict({"typo_field": 1}, env={})

    def test_bad_group_mode(self):
        with self.assertRaises(ValueError):
            config_from_dict({"group_mode": "everyone"}, env={})

    def test_ids_coerced_to_str(self):
        cfg = config_from_dict({"whitelist": [123], "blacklist": [456]}, env={})
        self.assertEqual(cfg.whitelist, ["123"])
        self.assertEqual(cfg.blacklist, ["456"])

    def test_text_limit(self):
        self.assertEqual(TEXT_LIMIT, 1800)


class _FakeDB:
    """只实现 resolve_name 用到的那两个方法。"""

    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = []
        self.nick_calls = 0

    def get_nickname(self, uid):
        self.nick_calls += 1
        return self.mapping.get(uid, uid)

    def search_contact(self, keyword):
        self.calls.append(keyword)
        return [{"username": "only_one"}]


class TestResolveName(unittest.TestCase):
    """发消息前要把群号换成群名，否则搜索框会先空搜一次。"""

    def _bot(self, db):
        from wx_bot import Bot
        return Bot(Config(), db=db, dry_run=True)

    def test_group_id_becomes_group_name(self):
        bot = self._bot(_FakeDB({"23136276396@chatroom": "产品交流群"}))
        self.assertEqual(bot.resolve_name("23136276396@chatroom"), "产品交流群")

    def test_friend_wxid_becomes_remark(self):
        bot = self._bot(_FakeDB({"wxid_abc": "张三"}))
        self.assertEqual(bot.resolve_name("wxid_abc"), "张三")

    def test_unknown_id_falls_back_to_itself(self):
        bot = self._bot(_FakeDB())
        self.assertEqual(bot.resolve_name("wxid_unknown"), "wxid_unknown")

    def test_result_is_cached(self):
        db = _FakeDB({"wxid_abc": "张三"})
        bot = self._bot(db)
        bot.resolve_name("wxid_abc")
        bot.resolve_name("wxid_abc")
        # 重名检查只应触发一次
        self.assertEqual(db.calls, ["张三"])

    def test_resolved_name_is_cached(self):
        db = _FakeDB({"wxid_abc": "张三"})
        bot = self._bot(db)
        bot.resolve_name("wxid_abc")
        bot.resolve_name("wxid_abc")
        self.assertEqual(db.nick_calls, 1, "查到名字后应该缓存，不要每条消息都查库")

    def test_unresolved_is_not_cached(self):
        """库里没名字的群（比如刚拉你进群还没打开过）不能把「群号」当成结果缓存，
        否则你在微信里给它加了备注也不会生效。"""
        db = _FakeDB()
        bot = self._bot(db)
        bot.resolve_name("45391456117@chatroom")
        bot.resolve_name("45391456117@chatroom")
        self.assertEqual(db.nick_calls, 2, "查不到名字时必须每次重查")

    def test_db_error_does_not_break_send(self):
        class Boom(_FakeDB):
            def get_nickname(self, uid):
                raise RuntimeError("库挂了")

        bot = self._bot(Boom())
        self.assertEqual(bot.resolve_name("wxid_abc"), "wxid_abc")

    def test_no_db(self):
        bot = self._bot(None)
        self.assertEqual(bot.resolve_name("wxid_abc"), "wxid_abc")


class TestPersona(unittest.TestCase):
    """人设从独立 txt 读，覆盖 system_prompt。"""

    def setUp(self):
        import pathlib
        self.here = str(pathlib.Path(__file__).parent)

    def test_file_overrides_prompt(self):
        from brain import apply_persona
        cfg = Config(system_prompt="旧人设", system_prompt_file="persona.txt")
        apply_persona(cfg, self.here)
        # 断言「等于文件内容」而不是某个角色名，换人设时不用改测试
        with open(pathlib.Path(self.here) / "persona.txt", encoding="utf-8-sig") as f:
            expected = f.read().strip()
        self.assertEqual(cfg.system_prompt, expected)
        self.assertNotEqual(cfg.system_prompt, "旧人设")

    def test_missing_file_raises(self):
        from brain import apply_persona
        cfg = Config(system_prompt_file="不存在的人设.txt")
        with self.assertRaises(ValueError):
            apply_persona(cfg, self.here)

    def test_no_file_key_keeps_prompt(self):
        from brain import apply_persona
        cfg = Config(system_prompt="保留我")
        apply_persona(cfg, self.here)
        self.assertEqual(cfg.system_prompt, "保留我")


class _FakeWx:
    """假的微信控制器：记录被哪个线程调用、按剧本决定成功还是抛错。"""

    class _Resp:
        is_success = True

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = []
        self.threads = set()

    def SendMsg(self, chunk, who=None):
        import threading
        self.calls.append((chunk, who))
        self.threads.add(threading.get_ident())
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("模拟 COMError: 事件无法调用任何订户")
        return self._Resp()


class TestSenderThread(unittest.TestCase):
    """发送必须收拢到一个线程：UIA 的 COM 对象是线程绑定的。"""

    def _bot(self, fake):
        from wx_bot import Bot
        bot = Bot(Config(), db=_FakeDB(), dry_run=False)
        bot._ensure_wx = lambda: fake
        return bot

    def test_two_chats_use_one_thread(self):
        import threading
        fake = _FakeWx()
        bot = self._bot(fake)

        ts = [threading.Thread(target=bot.send, args=(f"wxid_{i}", f"消息{i}"))
              for i in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)

        self.assertEqual(len(fake.calls), 3, "三条都应发出")
        self.assertEqual(len(fake.threads), 1, "三次发送应落在同一个线程")

    def test_retry_once_after_com_error(self):
        fake = _FakeWx(fail_times=1)
        bot = self._bot(fake)
        self.assertTrue(bot.send("wxid_a", "你好"), "第一次炸了应该重试成功")
        self.assertEqual(len(fake.calls), 2, "应该是「失败一次 + 成功一次」")

    def test_gives_up_after_two_failures(self):
        fake = _FakeWx(fail_times=5)
        bot = self._bot(fake)
        self.assertFalse(bot.send("wxid_a", "你好"))
        self.assertEqual(len(fake.calls), 2, "只重试一次，不无限重试")

    def test_dry_run_never_touches_wx(self):
        from wx_bot import Bot
        called = []
        bot = Bot(Config(), db=_FakeDB(), dry_run=True)
        bot._ensure_wx = lambda: called.append(1)
        self.assertTrue(bot.send("wxid_a", "你好"))
        self.assertEqual(called, [], "dry-run 不该初始化微信控制器")

    def test_multiline_sends_separate_messages(self):
        fake = _FakeWx()
        bot = self._bot(fake)
        with mock.patch("wx_bot.time.sleep"):      # 别为测试真的等 1.2 秒
            self.assertTrue(bot.send("wxid_a", "好呀\n那你早点睡"))
        self.assertEqual([c[0] for c in fake.calls], ["好呀", "那你早点睡"],
                         "多行要拆成两条，不能塞一条里")

    def test_split_can_be_turned_off(self):
        fake = _FakeWx()
        bot = self._bot(fake)
        bot.cfg.reply_split = False
        bot.send("wxid_a", "好呀\n那你早点睡")
        self.assertEqual([c[0] for c in fake.calls], ["好呀\n那你早点睡"])

    def test_tags_never_leak_into_messages(self):
        """拆条之后标签更容易漏出去，所以 send 自己再抠一遍。"""
        fake = _FakeWx()
        bot = self._bot(fake)
        bot.send("wxid_a", "好呀\n[好感:+3 被夸]\n[颜:开心]\n[回忆:心里软了一下]")
        self.assertEqual([c[0] for c in fake.calls], ["好呀"])


class TestStickers(unittest.TestCase):
    """表情包：模型点单 → 程序找文件 → 单独发一条。"""

    def setUp(self):
        import pathlib
        self.here = str(pathlib.Path(__file__).parent)

    def test_extract_simple(self):
        from brain import extract_stickers
        text, names = extract_stickers("好呀\n[表情:开心]")
        self.assertEqual(text, "好呀")
        self.assertEqual(names, ["开心"])

    def test_extract_fullwidth_brackets(self):
        from brain import extract_stickers
        text, names = extract_stickers("嗯……【表情：无语】")
        self.assertEqual(text, "嗯……")
        self.assertEqual(names, ["无语"])

    def test_extract_multiple_keeps_order(self):
        from brain import extract_stickers
        _, names = extract_stickers("[表情:开心] 好耶 [表情:晚安]")
        self.assertEqual(names, ["开心", "晚安"])

    def test_plain_text_untouched(self):
        from brain import extract_stickers
        text, names = extract_stickers("今天好累")
        self.assertEqual(text, "今天好累")
        self.assertEqual(names, [])

    def test_resolve_existing_file(self):
        from brain import resolve_sticker
        found = resolve_sticker(self.here, "persona.txt")
        self.assertIsNotNone(found)
        self.assertTrue(found.endswith("persona.txt"))

    def test_resolve_missing_returns_none(self):
        from brain import resolve_sticker
        self.assertIsNone(resolve_sticker(self.here, "根本不存在"))

    def test_path_traversal_is_blocked(self):
        from brain import resolve_sticker
        # 不能靠 ../ 把路径带出表情目录
        self.assertIsNone(resolve_sticker(self.here + "/stickers", "../persona.txt"))

    def test_empty_inputs(self):
        from brain import resolve_sticker
        self.assertIsNone(resolve_sticker("", "开心"))
        self.assertIsNone(resolve_sticker(self.here, ""))

    def test_list_stickers_missing_dir(self):
        from brain import list_stickers
        self.assertEqual(list_stickers(self.here + "/不存在目录"), [])

    def test_dry_run_sends_text_then_sticker(self):
        """dry-run 下应该先打文字，再说表情文件找到了没有。"""
        from wx_bot import Bot
        bot = Bot(Config(sticker_dir=self.here), db=_FakeDB(), dry_run=True)
        with self.assertLogs("wxbot", level="INFO") as cm:
            self.assertTrue(bot.send("wxid_a", "好呀\n[表情:persona.txt]"))
        joined = "\n".join(cm.output)
        self.assertIn("好呀", joined)
        self.assertIn("[表情:persona.txt]", joined)
        self.assertIn("persona.txt", joined)

    def test_dry_run_flags_missing_sticker(self):
        from wx_bot import Bot
        bot = Bot(Config(sticker_dir=self.here), db=_FakeDB(), dry_run=True)
        with self.assertLogs("wxbot", level="INFO") as cm:
            bot.send("wxid_a", "嗯\n[表情:没有这张]")
        self.assertIn("没找到这个文件", "\n".join(cm.output))


class TestMemoryPersistence(unittest.TestCase):
    """记忆落盘：写文件 → 新实例能读回来。"""

    def setUp(self):
        self.path = pathlib.Path(__file__).parent / "_test_memory.json"

    def tearDown(self):
        for p in (self.path, pathlib.Path(str(self.path) + ".tmp")):
            try:
                p.unlink()
            except OSError:
                pass

    def test_roundtrip_survives_restart(self):
        cfg = Config(history_turns=2)
        st = SessionStore(cfg, str(self.path))
        st.append("wxid_a", "你好", "你好呀")
        st.flush()

        st2 = SessionStore(cfg, str(self.path))
        msgs = st2.messages("wxid_a")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0], {"role": "user", "content": "你好"})
        self.assertEqual(msgs[1], {"role": "assistant", "content": "你好呀"})

    def test_history_is_trimmed_to_turns(self):
        cfg = Config(history_turns=1)     # 只记 1 轮 = 2 条
        st = SessionStore(cfg, str(self.path))
        for i in range(5):
            st.append("wxid_a", f"q{i}", f"a{i}")
        st.flush()
        msgs = SessionStore(cfg, str(self.path)).messages("wxid_a")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["content"], "q4")

    def test_expired_session_not_loaded(self):
        cfg = Config(session_ttl=1)
        st = SessionStore(cfg, str(self.path))
        st.append("wxid_a", "q", "a")
        st._data["wxid_a"]["ts"] = time.time() - 10   # 假装是 10 秒前
        st.flush()

        st2 = SessionStore(cfg, str(self.path))
        self.assertEqual(st2.messages("wxid_a"), [])

    def test_reset_persists(self):
        cfg = Config()
        st = SessionStore(cfg, str(self.path))
        st.append("wxid_a", "q", "a")
        st.flush()
        st.reset("wxid_a")
        self.assertEqual(SessionStore(cfg, str(self.path)).messages("wxid_a"), [])

    def test_corrupt_file_does_not_crash(self):
        self.path.write_text("{ 这不是 JSON", encoding="utf-8")
        st = SessionStore(Config(), str(self.path))
        self.assertEqual(st.messages("wxid_a"), [])

    def test_no_path_means_memory_only(self):
        st = SessionStore(Config())
        st.append("wxid_a", "q", "a")
        self.assertEqual(len(st.messages("wxid_a")), 2)

    def test_append_writes_and_flush_forces(self):
        cfg = Config()
        st = SessionStore(cfg, str(self.path))
        st.append("wxid_a", "q1", "a1")
        first = self.path.read_text(encoding="utf-8")
        st.append("wxid_a", "q2", "a2")          # 可能被 2 秒节流挡住
        st.flush()                                # 强制写
        second = self.path.read_text(encoding="utf-8")
        self.assertIn("q1", first)
        self.assertIn("q2", second)


class _FakeDB2:
    """用来测发送人解析：通讯录 + 群成员表。"""

    def __init__(self, contacts=None, groups=None):
        self.contacts = contacts or {}
        self.groups = groups or {}
        self.calls = 0

    def get_nickname(self, uid):
        self.calls += 1
        return self.contacts.get(uid, uid)

    def get_group_members(self, chat_id):
        return self.groups.get(chat_id, [])

    def search_contact(self, keyword):
        return []


class TestSenderName(unittest.TestCase):
    """识别消息发送人。"""

    def _bot(self, db):
        from wx_bot import Bot
        return Bot(Config(), db=db, dry_run=True)

    def test_contact_name(self):
        bot = self._bot(_FakeDB2(contacts={"wxid_a": "张三"}))
        self.assertEqual(
            bot.resolve_sender("wxid_chat", {"sender_username": "wxid_a"}), "张三")

    def test_group_member_not_in_contacts(self):
        db = _FakeDB2(groups={GROUP: [{"username": "wxid_b", "nick_name": "李四", "remark": ""}]})
        bot = self._bot(db)
        self.assertEqual(
            bot.resolve_sender(GROUP, {"sender_username": "wxid_b"}), "李四")

    def test_remark_beats_nickname(self):
        db = _FakeDB2(groups={GROUP: [{"username": "wxid_b", "nick_name": "李四", "remark": "老李"}]})
        self.assertEqual(
            self._bot(db).resolve_sender(GROUP, {"sender_username": "wxid_b"}), "老李")

    def test_unknown_sender_is_empty(self):
        self.assertEqual(
            self._bot(_FakeDB2()).resolve_sender("wxid_chat", {"sender_username": "wxid_z"}), "")

    def test_missing_sender_is_empty(self):
        self.assertEqual(self._bot(_FakeDB2()).resolve_sender("wxid_chat", {}), "")

    def test_result_is_cached(self):
        db = _FakeDB2(contacts={"wxid_a": "张三"})
        bot = self._bot(db)
        bot.resolve_sender("wxid_chat", {"sender_username": "wxid_a"})
        bot.resolve_sender("wxid_chat", {"sender_username": "wxid_a"})
        self.assertEqual(db.calls, 1)


class TestSpeakerInPrompt(unittest.TestCase):
    """发送人怎么进上下文。"""

    def test_group_prefixes_content(self):
        from brain import format_user_content
        cfg = Config()
        self.assertEqual(format_user_content(cfg, GROUP, "你好", "张三"), "张三：你好")

    def test_private_keeps_content_clean(self):
        from brain import format_user_content
        cfg = Config()
        self.assertEqual(format_user_content(cfg, "wxid_a", "你好", "张三"), "你好")

    def test_group_build_has_prefix(self):
        st = SessionStore(Config())
        msgs = st.build(GROUP, "你好", "张三")
        self.assertEqual(msgs[-1]["content"], "张三：你好")
        self.assertNotIn("当前对话", msgs[0]["content"])

    def test_private_build_mentions_who(self):
        st = SessionStore(Config())
        msgs = st.build("wxid_a", "你好", "张三")
        self.assertEqual(msgs[-1]["content"], "你好")
        self.assertIn("「张三」", msgs[0]["content"])

    def test_no_speaker_changes_nothing(self):
        st = SessionStore(Config())
        msgs = st.build("wxid_a", "你好", "")
        self.assertEqual(msgs[-1]["content"], "你好")
        self.assertNotIn("当前对话", msgs[0]["content"])


class TestDeveloper(unittest.TestCase):
    """开发者参数：命中就告诉模型「这是创造你的人」。"""

    def _bot(self, dev_ids):
        from wx_bot import Bot
        return Bot(Config(developer_ids=dev_ids), db=_FakeDB2(), dry_run=True)

    def test_match_by_wxid(self):
        bot = self._bot(["wxid_dev"])
        self.assertTrue(bot.is_developer(
            "wxid_dev", {"sender_username": "wxid_dev"}, ""))

    def test_match_by_name(self):
        bot = self._bot(["张三"])
        self.assertTrue(bot.is_developer(
            "wxid_chat", {"sender_username": "wxid_x"}, "张三"))

    def test_match_by_chat_id(self):
        # 私聊时 chat_id 就是对方的 wxid
        bot = self._bot(["wxid_dev"])
        self.assertTrue(bot.is_developer("wxid_dev", {"sender_username": ""}, ""))

    def test_no_match(self):
        bot = self._bot(["wxid_dev"])
        self.assertFalse(bot.is_developer(
            "wxid_other", {"sender_username": "wxid_other"}, "李四"))

    def test_empty_config_never_matches(self):
        bot = self._bot([])
        self.assertFalse(bot.is_developer("wxid_x", {"sender_username": "wxid_x"}, "x"))

    def test_case_insensitive(self):
        bot = self._bot(["WXID_DEV"])
        self.assertTrue(bot.is_developer("wxid_dev", {"sender_username": "wxid_dev"}, ""))

    def test_prompt_marks_developer(self):
        st = SessionStore(Config())
        msgs = st.build("wxid_dev", "在吗", "阿鱼", developer=True)
        self.assertIn("开发者本人", msgs[0]["content"])
        self.assertIn("「阿鱼」", msgs[0]["content"])

    def test_prompt_marks_developer_in_group_too(self):
        st = SessionStore(Config())
        msgs = st.build(GROUP, "在吗", "阿鱼", developer=True)
        self.assertIn("开发者本人", msgs[0]["content"])
        self.assertEqual(msgs[-1]["content"], "阿鱼：在吗")

    def test_prompt_quiet_for_normal_user(self):
        st = SessionStore(Config())
        msgs = st.build("wxid_x", "在吗", "李四", developer=False)
        self.assertNotIn("开发者", msgs[0]["content"])
        self.assertIn("一对一私聊", msgs[0]["content"])


class TestProfileMemory(unittest.TestCase):
    """长期记忆：姓名/id + 性格印象 + 有限的最近记录。"""

    def test_name_and_wxid_recorded(self):
        st = SessionStore(Config())
        st.touch_profile(FRIEND, name="张三", wxid=FRIEND)
        p = st.profile(FRIEND)
        self.assertEqual(p["name"], "张三")
        self.assertEqual(p["wxid"], FRIEND)

    def test_traits_deduped(self):
        st = SessionStore(Config())
        st.add_traits(FRIEND, ["喜欢猫", "说话短", "喜欢猫"])
        self.assertEqual(texts(st.profile(FRIEND)["traits"]), ["喜欢猫", "说话短"])

    def test_traits_capped(self):
        st = SessionStore(Config(memory_traits=3))
        st.add_traits(FRIEND, ["一", "二", "三", "四", "五"])
        traits = st.profile(FRIEND)["traits"]
        self.assertEqual(len(traits), 3)
        self.assertEqual(texts(traits)[-1], "五")      # 留最新的

    def test_trait_length_truncated(self):
        st = SessionStore(Config())
        st.add_traits(FRIEND, ["x" * 200])
        self.assertLessEqual(len(st.profile(FRIEND)["traits"][0]["text"]), 40)

    def test_recent_records_capped(self):
        st = SessionStore(Config(memory_recent=2))
        for i in range(5):
            st.append(FRIEND, f"q{i}", f"a{i}")
        recent = st.profile(FRIEND)["recent"]
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[-1]["u"], "q4")

    def test_prompt_includes_profile(self):
        st = SessionStore(Config())
        st.touch_profile(FRIEND, name="张三", wxid=FRIEND)
        st.add_traits(FRIEND, ["喜欢猫"])
        system = st.build(FRIEND, "在吗")[0]["content"]
        self.assertIn("【关于这个聊天对象】", system)
        self.assertIn("张三", system)
        self.assertIn("喜欢猫", system)

    def test_recap_only_for_fresh_session(self):
        st = SessionStore(Config())
        st.append(FRIEND, "q", "a")
        # 已有对话历史时不再重复讲「上次聊到」
        self.assertNotIn("上次聊到", st.build(FRIEND, "新")[0]["content"])

    def test_reset_keeps_profile(self):
        st = SessionStore(Config())
        st.touch_profile(FRIEND, name="张三")
        st.add_traits(FRIEND, ["喜欢猫"])
        st.append(FRIEND, "q", "a")
        st.reset(FRIEND)
        self.assertEqual(st.profile(FRIEND)["name"], "张三")
        self.assertEqual(texts(st.profile(FRIEND)["traits"]), ["喜欢猫"])

    def test_extract_notes(self):
        from brain import extract_notes
        text, notes = extract_notes("好呀\n[记住:喜欢猫]\n[记住: 在武汉上班]")
        self.assertEqual(text, "好呀")
        self.assertEqual(notes, ["喜欢猫", "在武汉上班"])

    def test_plain_reply_has_no_notes(self):
        from brain import extract_notes
        text, notes = extract_notes("今天好累")
        self.assertEqual((text, notes), ("今天好累", []))

    def test_profile_survives_restart(self):
        path = pathlib.Path(__file__).parent / "_test_profile.json"
        try:
            cfg = Config()
            st = SessionStore(cfg, str(path))
            st.touch_profile(FRIEND, name="张三", wxid=FRIEND)
            st.add_traits(FRIEND, ["喜欢猫"])
            st.append(FRIEND, "q", "a")
            st.flush()

            st2 = SessionStore(cfg, str(path))
            p = st2.profile(FRIEND)
            self.assertEqual(p["name"], "张三")
            self.assertEqual(texts(p["traits"]), ["喜欢猫"])
            self.assertEqual(len(p["recent"]), 1)
        finally:
            for p in (path, pathlib.Path(str(path) + ".tmp")):
                try:
                    p.unlink()
                except OSError:
                    pass


    def test_group_member_gets_own_profile(self):
        st = SessionStore(Config())
        st.touch_profile("wxid_a", name="张三", wxid="wxid_a")
        st.add_traits("wxid_a", ["喜欢猫"])
        system = st.build(GROUP, "在吗", "张三", person="wxid_a")[0]["content"]
        self.assertIn("张三", system)
        self.assertIn("喜欢猫", system)

    def test_two_members_have_separate_profiles(self):
        st = SessionStore(Config())
        st.touch_profile("wxid_a", name="张三")
        st.add_traits("wxid_a", ["喜欢猫"])
        st.touch_profile("wxid_b", name="李四")
        st.add_traits("wxid_b", ["爱打球"])

        a = st.build(GROUP, "我", "张三", person="wxid_a")[0]["content"]
        b = st.build(GROUP, "我", "李四", person="wxid_b")[0]["content"]
        self.assertIn("喜欢猫", a)
        self.assertNotIn("爱打球", a)
        self.assertIn("爱打球", b)
        self.assertNotIn("喜欢猫", b)

    def test_same_person_recognised_in_another_group(self):
        st = SessionStore(Config())
        st.touch_profile("wxid_a", name="张三")
        st.add_traits("wxid_a", ["喜欢猫"])
        system = st.build("9999@chatroom", "在吗", "张三", person="wxid_a")[0]["content"]
        self.assertIn("喜欢猫", system)

    def test_append_records_to_person_not_group(self):
        st = SessionStore(Config())
        st.append(GROUP, "张三：你好", "好呀", person="wxid_a")
        self.assertEqual(len(st.profile("wxid_a")["recent"]), 1)
        self.assertEqual(st.profile(GROUP), {}, "群本身不该再建档案")

    def test_append_falls_back_to_chat_id(self):
        st = SessionStore(Config())
        st.append(GROUP, "x", "y")          # 拿不到发送人时退回会话 id
        self.assertEqual(len(st.profile(GROUP)["recent"]), 1)

    def test_profile_key(self):
        from wx_bot import Bot
        bot = Bot(Config(), db=_FakeDB2(), dry_run=True)
        self.assertEqual(bot._profile_key(GROUP, {"sender_username": "wxid_a"}), "wxid_a")
        self.assertEqual(bot._profile_key(GROUP, {}), GROUP)
        self.assertEqual(bot._profile_key(FRIEND, {"sender_username": FRIEND}), FRIEND)

    def test_group_name_never_used_as_person_name(self):
        """解析不出群友名字时，不能把群名当成这个人叫啥。"""
        from wx_bot import Bot

        class Db(_FakeDB2):
            def get_nickname(self, uid):
                return uid          # 查不到

        db = Db()
        db.get_group_members = lambda cid: []
        bot = Bot(Config(), db=db, dry_run=True)
        bot._remember_who(GROUP, {"sender_username": "wxid_x"}, speaker="")
        self.assertEqual(bot.sessions.profile("wxid_x")["name"], "")

    def test_unknown_time_survives_load(self):
        """t=0 是「时间不明」的哨兵值，不能被当成假值替换成 now（否则显示成"刚刚"）。"""
        path = pathlib.Path(__file__).parent / "_test_legacy_time.json"
        try:
            path.write_text(json.dumps({
                "version": 2,
                "sessions": {},
                "profiles": {"wxid_a": {"name": "张三", "recent": [], "traits": [
                    {"t": 0, "w": 1, "hits": 0, "used": 0, "text": "老印象"}]}},
            }, ensure_ascii=False), encoding="utf-8")
            st = SessionStore(Config(), str(path))
            self.assertEqual(st.profile("wxid_a")["traits"][0]["t"], 0)
            self.assertIn("· 之前 · 老印象", st.profile_text("wxid_a"))
        finally:
            for p in (path, pathlib.Path(str(path) + ".tmp")):
                try:
                    p.unlink()
                except OSError:
                    pass

    def test_old_group_profiles_dropped_on_load(self):
        path = pathlib.Path(__file__).parent / "_test_legacy.json"
        try:
            path.write_text(json.dumps({
                "version": 2,
                "sessions": {},
                "profiles": {
                    GROUP: {"name": "某群", "traits": ["群梗"], "recent": []},
                    "wxid_a": {"name": "张三", "traits": ["喜欢猫"], "recent": []},
                },
            }, ensure_ascii=False), encoding="utf-8")
            st = SessionStore(Config(), str(path))
            self.assertEqual(st.profile(GROUP), {}, "旧版按群建的档案应该被丢掉")
            self.assertEqual(st.profile("wxid_a")["name"], "张三")
            # 老档案里的字符串印象要自动补成 {t, w, text}，不能丢
            migrated = st.profile("wxid_a")["traits"]
            self.assertEqual(texts(migrated), ["喜欢猫"])
            self.assertIsInstance(migrated[0], dict)
            self.assertEqual(migrated[0]["w"], 1)
        finally:
            for p in (path, pathlib.Path(str(path) + ".tmp")):
                try:
                    p.unlink()
                except OSError:
                    pass


class TestLongMemory(unittest.TestCase):
    """长期记忆 A+B：存更多条（带时间/权重），但每条消息只注入最相关的几条。"""

    def _st(self, **kw):
        return SessionStore(Config(**kw))

    # -- 相似去重：同一件事不再占两个位置 --

    def test_similar_traits_merge(self):
        st = self._st()
        st.add_traits(FRIEND, ["喜欢猫"])
        st.add_traits(FRIEND, ["他喜欢猫"])
        traits = st.profile(FRIEND)["traits"]
        self.assertEqual(len(traits), 1)
        self.assertEqual(traits[0]["text"], "他喜欢猫", "内容取新的")

    def test_unrelated_traits_stay_apart(self):
        st = self._st()
        st.add_traits(FRIEND, ["喜欢猫", "对花生过敏"])
        self.assertEqual(len(st.profile(FRIEND)["traits"]), 2)

    def test_trait_similar_helper(self):
        from brain import trait_similar
        self.assertGreaterEqual(trait_similar("喜欢猫", "他喜欢猫"), 0.6)
        self.assertLess(trait_similar("对花生过敏", "对海鲜过敏"), 0.6)

    def test_repeat_raises_weight(self):
        st = self._st()
        for _ in range(3):
            st.add_traits(FRIEND, ["对花生过敏"])
        item = st.profile(FRIEND)["traits"][0]
        self.assertEqual(item["w"], 3)
        self.assertEqual(len(st.profile(FRIEND)["traits"]), 1)

    # -- 淘汰：重要的不再被闲聊挤掉 --

    def test_important_trait_survives_overflow(self):
        st = self._st(memory_traits=3)
        st.add_traits(FRIEND, ["对花生过敏"])
        st.add_traits(FRIEND, ["对花生过敏"])          # 提了两次 → 权重 2
        for i in range(5):
            st.add_traits(FRIEND, [f"闲聊{i}"])
        kept = texts(st.profile(FRIEND)["traits"])
        self.assertIn("对花生过敏", kept, "重要的不该被闲聊挤掉")
        self.assertEqual(len(kept), 3)
        self.assertNotIn("闲聊0", kept)

    # -- 检索 --

    def test_recall_picks_relevant(self):
        st = self._st()
        for t in ["对花生过敏", "在武汉读研", "养了三花猫", "喜欢打羽毛球"]:
            st.add_traits(FRIEND, [t])
        hit = st.recall(FRIEND, "我吃了花生，过敏了")
        self.assertEqual(hit[0]["text"], "对花生过敏")

    def test_recall_limit(self):
        st = self._st(memory_recall=2)
        for i in range(10):
            st.add_traits(FRIEND, [f"第{i}件事"])
        self.assertEqual(len(st.recall(FRIEND, "第3件事")), 2)

    def test_recall_budget(self):
        st = self._st(memory_recall=10, memory_recall_chars=60)
        for i in range(10):
            st.add_traits(FRIEND, ["很长的印象%d" % i * 3])
        self.assertLessEqual(len(st.recall(FRIEND, "印象")), 2)

    def test_recall_falls_back_to_newest(self):
        st = self._st(memory_recall=2)
        for t in ["一", "二", "三", "四"]:
            st.add_traits(FRIEND, [t])
        hit = st.recall(FRIEND, "晚安")
        self.assertEqual(sorted(h["text"] for h in hit), ["三", "四"])

    def test_recall_marks_usage(self):
        st = self._st()
        st.add_traits(FRIEND, ["对花生过敏"])
        st.recall(FRIEND, "花生")
        item = st.profile(FRIEND)["traits"][0]
        self.assertEqual(item["hits"], 1)
        self.assertGreater(item["used"], 0)

    def test_prompt_injects_only_recalled(self):
        st = self._st(memory_recall=2)
        st.add_traits(FRIEND, ["对花生过敏"])
        for i in range(11):
            st.add_traits(FRIEND, [f"闲聊第{i}件"])
        system = st.build(FRIEND, "我吃了花生，过敏了")[0]["content"]
        self.assertIn("对花生过敏", system, "相关的那条要在")
        self.assertEqual(system.count("闲聊"), 1, "12 条里只挑 2 条，不能全塞")

    def test_prompt_uses_relative_time(self):
        st = self._st()
        st.touch_profile(FRIEND, name="阿鱼")
        st.add_traits(FRIEND, ["对花生过敏"])
        system = st.build(FRIEND, "记性真差")[0]["content"]
        self.assertIn("刚刚 · 对花生过敏", system)

    def test_empty_profile_still_clean(self):
        st = self._st()
        st.touch_profile(FRIEND, name="阿鱼")
        system = st.build(FRIEND, "在吗")[0]["content"]
        self.assertNotIn("你记得的事", system)


class TestRelativeTime(unittest.TestCase):
    def test_buckets(self):
        from brain import relative_time
        now = 1000000000.0
        self.assertEqual(relative_time(now - 60, now), "刚刚")
        self.assertEqual(relative_time(now - 3 * 3600, now), "今天")
        self.assertEqual(relative_time(now - 30 * 3600, now), "昨天")
        self.assertEqual(relative_time(now - 3 * 86400, now), "3天前")
        self.assertEqual(relative_time(now - 14 * 86400, now), "2周前")
        self.assertEqual(relative_time(now - 70 * 86400, now), "2个月前")
        self.assertEqual(relative_time(now - 400 * 86400, now), "很久以前")

    def test_bad_input(self):
        from brain import relative_time
        self.assertEqual(relative_time("坏值"), "之前")
        self.assertEqual(relative_time(0), "之前", "老档案没时间，别显示成刚刚")


class TestVision(unittest.TestCase):
    """识图：带图那一轮构造数组 content，并切到视觉模型。"""

    def setUp(self):
        self.here = pathlib.Path(__file__).parent
        self.img = self.here / "stickers" / "示例.jpg"

    def test_build_puts_image_in_user_message(self):
        st = SessionStore(Config(vision_detail="low"))
        msgs = st.build(FRIEND, "这是什么", "", False, FRIEND, "data:image/jpeg;base64,AAA")
        content = msgs[-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["detail"], "low")

    def test_build_without_image_stays_string(self):
        st = SessionStore(Config())
        msgs = st.build(FRIEND, "这是什么", "", False, FRIEND, None)
        self.assertIsInstance(msgs[-1]["content"], str)

    def test_image_only_in_user_message(self):
        st = SessionStore(Config())
        msgs = st.build(FRIEND, "看图", "", False, FRIEND, "data:image/jpeg;base64,AAA")
        for m in msgs[:-1]:
            self.assertIsInstance(m["content"], str, "system/历史里不能出现图片块")

    def test_encode_image_returns_data_url(self):
        if not self.img.exists():
            self.skipTest("没有测试图片")
        from brain import encode_image
        url = encode_image(str(self.img))
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertGreater(len(url), 100)

    def test_sniff_mime(self):
        from brain import sniff_image_mime
        self.assertEqual(sniff_image_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(sniff_image_mime(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(sniff_image_mime(b"GIF89a...."), "image/gif")
        self.assertEqual(sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertEqual(sniff_image_mime(b"wxgf\x13\x00"), "")

    def test_wxgf_is_rejected_not_sent(self):
        """微信的 .wxgf 不是图片：必须报错，绝不能当成 jpeg 发出去（线上 400 的根因）。"""
        from brain import UnsupportedImage, encode_image
        bad = pathlib.Path(__file__).parent / "_test_bad.wxgf"
        try:
            bad.write_bytes(b"wxgf" + b"\x00" * 64)
            with self.assertRaises(UnsupportedImage):
                encode_image(str(bad))
        finally:
            try:
                bad.unlink()
            except OSError:
                pass

    def test_chat_uses_given_model(self):
        calls = {}

        def transport(url, payload, headers, timeout):
            calls.update(payload)
            return {"choices": [{"message": {"content": "看到了"}}]}

        client = DeepSeekClient(Config(api_key="sk-x"), transport=transport)
        client.chat([{"role": "user", "content": "hi"}], model="deepseek-flash")
        self.assertEqual(calls["model"], "deepseek-flash")
        self.assertIn("temperature", calls)


class TestImageCache(unittest.TestCase):
    """图片缓存：等后面那条文字/@ 来认领。"""

    def _bot(self, **kw):
        from wx_bot import Bot
        return Bot(Config(**kw), db=_FakeDB2(), dry_run=True)

    def _msg(self, local_id, sender="wxid_a", chat=FRIEND):
        return {"type": "图片", "username": chat, "sender_username": sender,
                "local_id": local_id, "shard": "message_0.db"}

    def test_remember_and_take(self):
        bot = self._bot()
        bot._remember_image(self._msg(1))
        rec = bot._take_recent_image(FRIEND, "wxid_a")
        self.assertEqual(rec["local_id"], 1)
        self.assertIsNone(bot._take_recent_image(FRIEND, "wxid_a"), "一图只用一次")

    def test_only_same_sender(self):
        bot = self._bot()
        bot._remember_image(self._msg(1, sender="wxid_a"))
        self.assertIsNone(bot._take_recent_image(FRIEND, "wxid_b"))

    def test_expired_image_ignored(self):
        bot = self._bot(vision_image_ttl=0.1)
        bot._remember_image(self._msg(1))
        time.sleep(0.15)
        self.assertIsNone(bot._take_recent_image(FRIEND, "wxid_a"))

    def test_takes_newest_first(self):
        bot = self._bot()
        bot._remember_image(self._msg(1))
        bot._remember_image(self._msg(2))
        self.assertEqual(bot._take_recent_image(FRIEND, "wxid_a")["local_id"], 2)

    def test_cache_capped(self):
        bot = self._bot()
        for i in range(9):
            bot._remember_image(self._msg(i))
        self.assertEqual(len(bot._recent_images[FRIEND]), 5)

    def test_image_message_does_not_reply(self):
        bot = self._bot()
        bot.on_message(self._msg(1))       # 不该抛异常，也不该回
        self.assertEqual(len(bot._recent_images[FRIEND]), 1)


class TestCurrentTime(unittest.TestCase):
    """把当前时间注入 system —— 模型自己没有时钟。"""

    def _ts(self, hour=10, minute=23):
        return time.mktime((2026, 10, 24, hour, minute, 0, 0, 0, -1))

    def test_period_of_day(self):
        from brain import period_of_day
        cases = {0: "凌晨", 4: "凌晨", 5: "早上", 7: "早上", 8: "上午",
                 11: "中午", 12: "中午", 13: "下午", 17: "下午",
                 18: "晚上", 22: "晚上", 23: "深夜"}
        for hour, want in cases.items():
            self.assertEqual(period_of_day(hour), want, f"{hour} 点")

    def test_now_line_format(self):
        from brain import now_line
        line = now_line(self._ts())
        self.assertIn("2026年10月24日", line)
        self.assertIn("上午", line)
        self.assertIn("10:23", line)
        self.assertIn("北京时间", line)
        self.assertTrue(line.startswith("【现在】"))

    def test_now_line_uses_local_time(self):
        from brain import now_line
        t = time.localtime(self._ts(15))
        self.assertIn("下午", now_line(self._ts(15)))
        self.assertIn("%d月" % t.tm_mon, now_line(self._ts(15)))

    def test_build_injects_time_into_system(self):
        st = SessionStore(Config())
        msgs = st.build(FRIEND, "现在几点")
        self.assertIn("【现在】", msgs[0]["content"])
        # 时间放在 system 的最末尾（前面人设保持稳定，缓存不受影响）
        self.assertTrue(msgs[0]["content"].rstrip().endswith("（北京时间）"))

    def test_time_not_in_user_message(self):
        st = SessionStore(Config())
        msgs = st.build(FRIEND, "现在几点")
        self.assertEqual(msgs[-1]["content"], "现在几点")


class TestModelSwitch(unittest.TestCase):
    """deepseek-flash 默认开思考，聊天场景要显式关掉。"""

    def _client(self, **kw):
        calls = {}

        def transport(url, payload, headers, timeout):
            calls.update(payload)
            return {"choices": [{"message": {"content": "ok"}}]}

        return DeepSeekClient(Config(api_key="sk-x", **kw), transport=transport), calls

    def test_default_is_flash_with_thinking_off(self):
        c, calls = self._client()
        c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(calls["model"], "deepseek-flash")
        self.assertEqual(calls["thinking"], {"type": "disabled"})
        self.assertIn("temperature", calls, "关掉思考后才带 temperature")

    def test_thinking_on_drops_temperature(self):
        c, calls = self._client(thinking=True)
        c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(calls["thinking"], {"type": "enabled"})
        self.assertNotIn("temperature", calls, "思考模式不支持 temperature")

    def test_legacy_model_gets_no_thinking_param(self):
        c, calls = self._client(model="deepseek-chat")
        c.chat([{"role": "user", "content": "hi"}])
        self.assertNotIn("thinking", calls, "老模型不认这个参数")
        self.assertIn("temperature", calls)

    def test_vision_call_also_disables_thinking(self):
        c, calls = self._client()
        c.chat([{"role": "user", "content": "看图"}], model="deepseek-flash")
        self.assertEqual(calls["thinking"], {"type": "disabled"})


class TestConnectBudget(unittest.TestCase):
    """建连限时：一个不回包的 CDN 地址不能把整条回复拖到 22 秒（线上根因）。"""

    def setUp(self):
        import brain
        self.brain = brain
        brain._addr_cache.clear()

    def tearDown(self):
        self.brain._addr_cache.clear()

    @staticmethod
    def _fake_connect(dead):
        tried = []

        class Sock:
            def settimeout(self, t):
                pass

            def close(self):
                pass

        def create(addr, timeout=None):
            tried.append(addr[0])
            if addr[0] in dead:
                raise TimeoutError("no SYN reply")
            return Sock()

        return tried, create, Sock

    def test_skips_dead_address_and_caches_good_one(self):
        tried, create, Sock = self._fake_connect({"192.0.2.1"})
        with mock.patch.object(self.brain, "_resolve_ips",
                               lambda h, p: ["192.0.2.1", "10.0.0.9"]), \
             mock.patch.object(self.brain.socket, "create_connection", create):
            sock = self.brain._connect("api.deepseek.com", 443, 60)
        self.assertIsInstance(sock, Sock)
        self.assertEqual(tried, ["192.0.2.1", "10.0.0.9"], "第一个不通就该换下一个")
        self.assertEqual(self.brain._addr_cache["api.deepseek.com"][0], "10.0.0.9")

    def test_cached_ip_is_tried_first(self):
        tried, create, _ = self._fake_connect(set())
        self.brain._addr_cache["api.deepseek.com"] = ("10.0.0.9", time.time())
        with mock.patch.object(self.brain, "_resolve_ips", lambda h, p: ["192.0.2.1"]), \
             mock.patch.object(self.brain.socket, "create_connection", create):
            self.brain._connect("api.deepseek.com", 443, 60)
        self.assertEqual(tried[0], "10.0.0.9")

    def test_dead_cached_ip_is_dropped(self):
        _, create, _ = self._fake_connect({"10.0.0.9"})
        self.brain._addr_cache["api.deepseek.com"] = ("10.0.0.9", time.time())
        with mock.patch.object(self.brain, "_resolve_ips", lambda h, p: ["1.2.3.4"]), \
             mock.patch.object(self.brain.socket, "create_connection", create):
            self.brain._connect("api.deepseek.com", 443, 60)
        self.assertEqual(self.brain._addr_cache["api.deepseek.com"][0], "1.2.3.4")

    def test_all_dead_raises_connect_error(self):
        _, create, _ = self._fake_connect({"192.0.2.1", "10.0.0.9"})
        with mock.patch.object(self.brain, "_resolve_ips",
                               lambda h, p: ["192.0.2.1", "10.0.0.9"]), \
             mock.patch.object(self.brain.socket, "create_connection", create):
            with self.assertRaises(self.brain._ConnectError):
                self.brain._connect("api.deepseek.com", 443, 60)

    def test_no_address_raises(self):
        with mock.patch.object(self.brain, "_resolve_ips", lambda h, p: []):
            with self.assertRaises(self.brain._ConnectError):
                self.brain._connect("api.deepseek.com", 443, 60)

    def test_connection_class_uses_budget_connect(self):
        conn = self.brain._BudgetConnection("api.deepseek.com")
        self.assertIs(conn._create_connection.__func__,
                      self.brain._BudgetConnection._connect_with_budget)


class TestRequestRetry(unittest.TestCase):
    """只重试「一个字节都没发出去」的失败；HTTP 错误和读超时都不重试。"""

    class _Resp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def _opener(self, script):
        calls = []
        outer = self

        class Opener:
            def open(self, req, timeout=None):
                calls.append(req.full_url)
                item = script[len(calls) - 1]
                if isinstance(item, Exception):
                    raise item
                return outer._Resp(json.dumps(item).encode())

        return Opener(), calls

    def test_retries_once_on_connect_error(self):
        import brain
        err = urllib.error.URLError(brain._ConnectError("全挂了"))
        op, calls = self._opener([err, {"ok": 1}])
        with mock.patch.object(brain, "_opener", lambda: op):
            self.assertEqual(brain._post_json("https://x/y", {}, {}, 60), {"ok": 1})
        self.assertEqual(len(calls), 2)

    def test_does_not_retry_http_error(self):
        import brain
        err = urllib.error.HTTPError("https://x", 401, "unauthorized", {},
                                     io.BytesIO(b'{"error":"bad key"}'))
        op, calls = self._opener([err])
        with mock.patch.object(brain, "_opener", lambda: op):
            with self.assertRaises(DeepSeekError) as ctx:
                brain._post_json("https://x/y", {}, {}, 60)
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(calls), 1, "认证失败重试也没用，别浪费一次")

    def test_does_not_retry_read_timeout(self):
        import brain
        err = urllib.error.URLError(TimeoutError("read timeout"))
        op, calls = self._opener([err])
        with mock.patch.object(brain, "_opener", lambda: op):
            with self.assertRaises(DeepSeekError):
                brain._post_json("https://x/y", {}, {}, 60)
        self.assertEqual(len(calls), 1, "请求可能已经发出去了，重试会重复计费")


class TestProactive(unittest.TestCase):
    """主动说话（实验）：灰度、初筛、冷却、配额、抽查，缺一不可。"""

    def _bot(self, reply="[不说]", **kw):
        from wx_bot import Bot
        cfg = Config(api_key="sk-test", **kw)
        bot = Bot(cfg, db=None, dry_run=True)
        sent = []
        calls = []

        def fake(url, payload, headers, timeout):
            calls.append(payload)
            return {"choices": [{"message": {"content": reply}}]}

        bot.send = lambda chat_id, text: sent.append((chat_id, text)) or True
        bot.llm = DeepSeekClient(cfg, transport=fake)
        return bot, sent, calls

    def _msg(self, text="今天有人一起打球吗，三缺一", chat=GROUP, sender="wxid_bob"):
        return msg(text, chat=chat, sender=sender)

    def _on(self, **kw):
        base = dict(proactive_enabled=True, proactive_ids=[GROUP],
                    proactive_chance=1.0)
        base.update(kw)
        return base

    def test_off_by_default(self):
        bot, sent, calls = self._bot()
        bot.on_message(self._msg())
        self.assertEqual((calls, sent), ([], []), "默认关闭，一个 API 都不该调")

    def test_empty_gray_list_means_nobody(self):
        bot, sent, calls = self._bot(proactive_enabled=True, proactive_ids=[])
        bot.on_message(self._msg())
        self.assertEqual((calls, sent), ([], []))

    def test_chat_not_in_gray(self):
        bot, sent, calls = self._bot(**self._on(proactive_ids=["别的群"]))
        bot.on_message(self._msg())
        self.assertEqual((calls, sent), ([], []))

    def test_declines_and_sends_nothing(self):
        bot, sent, calls = self._bot("[不说]", **self._on())
        bot.on_message(self._msg())
        self.assertEqual(len(calls), 1, "问一次该不该说")
        self.assertEqual(sent, [])

    def test_speaks_when_model_wants_to(self):
        bot, sent, calls = self._bot("三缺一？我来凑个数", **self._on())
        bot.on_message(self._msg())
        self.assertEqual(len(calls), 1)
        self.assertEqual(sent, [(GROUP, "三缺一？我来凑个数")])

    def test_reply_is_recorded_in_history(self):
        bot, sent, calls = self._bot("我也想去", **self._on())
        bot.on_message(self._msg())
        msgs = bot.sessions.messages(GROUP)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[-1]["content"], "我也想去")

    def test_short_message_not_judged(self):
        bot, sent, calls = self._bot("三缺一", **self._on())
        bot.on_message(self._msg("嗯"))          # 太短，不值得花一次 API
        self.assertEqual(calls, [])

    def test_laughter_not_judged(self):
        bot, sent, calls = self._bot("乐到了吧", **self._on())
        for text in ("哈哈哈哈哈", "笑死我了", "23333", "草"):
            bot.on_message(self._msg(text))
        self.assertEqual(calls, [], "纯笑声/凑数的不接")

    def test_cooldown_blocks_second(self):
        bot, sent, calls = self._bot("我来", **self._on())
        bot.on_message(self._msg())
        bot.on_message(self._msg("还有人吗，快点"))
        self.assertEqual(len(calls), 1, "两次主动发言之间至少隔 proactive_min_gap")
        self.assertEqual(len(sent), 1)

    def test_hourly_cap(self):
        bot, sent, calls = self._bot("我来", **self._on(
            proactive_min_gap=0, proactive_max_per_hour=1))
        bot.on_message(self._msg())
        bot.on_message(self._msg("还有人吗，快点"))
        self.assertEqual(len(calls), 1, "一小时只有 1 次配额")

    def test_chance_zero_never_judges(self):
        bot, sent, calls = self._bot("我来", **self._on(proactive_chance=0.0))
        for _ in range(5):
            bot.on_message(self._msg())
        self.assertEqual(calls, [])

    def test_gray_by_sender_wxid(self):
        bot, sent, calls = self._bot("我懂这个", **self._on(
            proactive_ids=["wxid_bob"], proactive_chance=1.0))
        bot.on_message(self._msg())
        self.assertEqual(len(calls), 1, "名单里写人名/群友 wxid 也算命中")

    def test_gray_by_chat_name(self):
        bot, sent, calls = self._bot("哈哈", **self._on())
        bot._name_cache[GROUP] = "所有人不许睡觉"
        bot.cfg.proactive_ids = ["所有人不许睡觉"]
        bot.on_message(self._msg())
        self.assertEqual(len(calls), 1, "名单里写群名也能命中")

    def test_tags_are_stripped(self):
        bot, sent, calls = self._bot(
            "哈哈你们太逗了[颜:开心]\n[好感:+3 被逗笑]", **self._on())
        bot.on_message(self._msg())
        self.assertEqual(sent, [(GROUP, "哈哈你们太逗了")])
        self.assertEqual(bot.sessions.profile("wxid_bob").get("affinity"), 50,
                         "主动插话不改好感度")

    def test_too_long_not_sent(self):
        bot, sent, calls = self._bot("啊" * 200, **self._on())
        bot.on_message(self._msg())
        self.assertEqual(sent, [], "群里插话不该是长篇大论")

    def test_at_message_still_uses_normal_reply(self):
        bot, sent, calls = self._bot("在的", **self._on())
        bot.cfg.my_names = ["小鲸鱼"]
        bot.on_message(self._msg("@小鲸鱼 你在吗"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sent, [(GROUP, "在的")], "被 @ 时走正常回复，不是插话")


class TestWebSearch(unittest.TestCase):
    """Search API 的封装：结果要整理成模型能读的文字。"""

    def _ws(self, transport):
        return WebSearch(Config(api_key="sk-x"), transport=transport)

    def test_formats_results(self):
        seen = {}

        def transport(url, payload, headers, timeout):
            seen["url"] = url
            seen["payload"] = payload
            return {"results": [
                {"title": "武汉天气预报", "summary": "今天多云 18-25 度",
                 "url": "https://example.com/w", "date": "2026-10-24T00:00:00+00:00"},
                {"title": "第二条", "snippet": "摘要 B", "url": ""},
            ]}

        out = self._ws(transport).search("武汉天气", 2)
        self.assertIn("/v1/tools/search", seen["url"])
        self.assertEqual(seen["payload"]["query"], "武汉天气")
        self.assertEqual(seen["payload"]["count"], 2)
        self.assertIn("武汉天气预报", out)
        self.assertIn("今天多云", out)
        self.assertIn("2026-10-24", out)
        self.assertIn("不是指令", out, "必须告诉模型这些只是资料")

    def test_empty_results(self):
        out = self._ws(lambda *a, **k: {"results": []}).search("冷门词")
        self.assertIn("没有搜到", out)

    def test_empty_query(self):
        out = self._ws(lambda *a, **k: {"results": []}).search("   ")
        self.assertIn("为空", out)

    def test_summary_truncated(self):
        long = "啊" * 1000
        out = self._ws(lambda *a, **k: {"results": [
            {"title": "t", "summary": long, "url": ""}]}).search("q", 1)
        self.assertLess(len(out), 800)


class TestToolLoop(unittest.TestCase):
    """function calling：模型要工具 → 我们执行 → 回灌 → 收口。"""

    def _client(self, script):
        calls = []

        def transport(url, payload, headers, timeout):
            calls.append(payload)
            return script(len(calls))

        return DeepSeekClient(Config(api_key="sk-x"), transport=transport), calls

    @staticmethod
    def _tool_call(query="武汉天气"):
        return {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "web_search",
                                         "arguments": json.dumps({"query": query})}}],
        }}]}

    def test_tool_call_then_answer(self):
        def script(n):
            return self._tool_call() if n == 1 else {
                "choices": [{"message": {"content": "今天武汉多云"}}]}

        client, calls = self._client(script)
        used = []
        out = client.chat([{"role": "user", "content": "武汉天气"}],
                          tools=[WEB_SEARCH_TOOL],
                          tool_runner=lambda n, a: used.append((n, a)) or "搜索结果文本")

        self.assertEqual(out, "今天武汉多云")
        self.assertEqual(len(calls), 2)
        self.assertEqual(used[0][0], "web_search")
        self.assertIn('"query"', used[0][1])
        self.assertEqual(calls[0]["tools"], [WEB_SEARCH_TOOL])
        roles = [m["role"] for m in calls[1]["messages"]]
        self.assertEqual(roles[-2:], ["assistant", "tool"])
        self.assertEqual(calls[1]["messages"][-1]["content"], "搜索结果文本")

    def test_tool_error_is_fed_back(self):
        def script(n):
            return self._tool_call() if n == 1 else {
                "choices": [{"message": {"content": "没搜到，直接答"}}]}

        def boom(name, args):
            raise RuntimeError("搜索炸了")

        client, calls = self._client(script)
        out = client.chat([{"role": "user", "content": "x"}],
                          tools=[WEB_SEARCH_TOOL], tool_runner=boom)
        self.assertEqual(out, "没搜到，直接答")
        self.assertIn("工具执行失败", calls[1]["messages"][-1]["content"])

    def test_rounds_limit_then_final_without_tools(self):
        def script(n):
            if n <= 2:
                return self._tool_call()
            return {"choices": [{"message": {"content": "最后一次直接答"}}]}

        client, calls = self._client(script)
        cfg_rounds = client.cfg.web_search_rounds
        self.assertEqual(cfg_rounds, 2)
        out = client.chat([{"role": "user", "content": "x"}],
                          tools=[WEB_SEARCH_TOOL],
                          tool_runner=lambda n, a: "结果")
        self.assertEqual(out, "最后一次直接答")
        self.assertEqual(len(calls), 3)
        self.assertNotIn("tools", calls[-1], "最后一轮必须不带工具，逼它收口")

    def test_no_tools_single_request(self):
        client, calls = self._client(
            lambda n: {"choices": [{"message": {"content": "直接答"}}]})
        out = client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(out, "直接答")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", calls[0])

    def test_history_not_mutated(self):
        def script(n):
            return self._tool_call() if n == 1 else {
                "choices": [{"message": {"content": "ok"}}]}

        client, _ = self._client(script)
        msgs = [{"role": "user", "content": "x"}]
        client.chat(msgs, tools=[WEB_SEARCH_TOOL], tool_runner=lambda n, a: "r")
        self.assertEqual(len(msgs), 1, "不能污染调用方的会话历史")


class TestAffinity(unittest.TestCase):
    """好感度：分档 + 三道闸（单次/每日/总分）。"""

    def _st(self):
        return SessionStore(Config())

    def test_tiers(self):
        from brain import tier_of
        cases = {0: "冷淡", 20: "冷淡", 21: "客气", 40: "客气", 41: "眼熟",
                 60: "眼熟", 61: "熟络", 80: "熟络", 81: "亲密", 100: "亲密"}
        for score, want in cases.items():
            self.assertEqual(tier_of(score)[0], want, f"{score} 分")

    def test_tier_clamps_input(self):
        from brain import tier_of
        self.assertEqual(tier_of(-50)[0], "冷淡")
        self.assertEqual(tier_of(999)[0], "亲密")
        self.assertEqual(tier_of("坏数据")[0], "眼熟", "非法输入回落到初值档")

    def test_initial_value(self):
        st = self._st()
        st.touch_profile(FRIEND, name="阿鱼")
        self.assertEqual(st.profile(FRIEND)["affinity"], 50, "新朋友从中性 50 开始")

    def test_single_step_capped(self):
        st = self._st()
        st.touch_profile(FRIEND)
        self.assertEqual(st.bump_affinity(FRIEND, +100), 60, "单次最多 +10")
        self.assertEqual(st.bump_affinity(FRIEND, -100), 50, "单次最多 -10")

    def test_daily_up_cap(self):
        st = self._st()
        st.touch_profile(FRIEND)
        for _ in range(5):
            st.bump_affinity(FRIEND, +10)
        self.assertEqual(st.profile(FRIEND)["affinity"], 80, "每天最多 +30")

    def test_daily_down_cap(self):
        st = self._st()
        st.touch_profile(FRIEND)
        st._profiles[FRIEND]["affinity"] = 80
        for _ in range(8):
            st.bump_affinity(FRIEND, -10)
        self.assertEqual(st.profile(FRIEND)["affinity"], 40, "每天最多 -40")

    def test_floor_and_ceiling(self):
        st = self._st()
        st.touch_profile(FRIEND)
        st._profiles[FRIEND]["affinity"] = 3
        st.bump_affinity(FRIEND, -10)
        self.assertEqual(st.profile(FRIEND)["affinity"], 0, "不能低于 0")

        st2 = self._st()
        st2.touch_profile(FRIEND)
        st2._profiles[FRIEND]["affinity"] = 98
        st2.bump_affinity(FRIEND, +10)
        self.assertEqual(st2.profile(FRIEND)["affinity"], 100, "不能高于 100")

    def test_new_day_resets_quota(self):
        st = self._st()
        st.touch_profile(FRIEND)
        for _ in range(2):
            st.bump_affinity(FRIEND, +10)
        self.assertEqual(st.profile(FRIEND)["affinity"], 70)
        st._profiles[FRIEND]["affinity_day"] = "1999-01-01"   # 假装到了新的一天
        st.bump_affinity(FRIEND, +10)
        self.assertEqual(st.profile(FRIEND)["affinity"], 80)

    def test_changes_are_logged(self):
        st = self._st()
        st.touch_profile(FRIEND)
        st.bump_affinity(FRIEND, +3, "被夸")
        log = st.profile(FRIEND)["affinity_log"]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["d"], 3)
        self.assertEqual(log[0]["why"], "被夸")

    def test_profile_text_shows_relation(self):
        st = self._st()
        st.touch_profile(FRIEND, name="阿鱼")
        text = st.profile_text(FRIEND)
        self.assertIn("关系：眼熟（50/100）", text)
        self.assertIn("偶尔卖个萌", text, "要带上该档位的行为提示")

    def test_query_command_reports_exact_score(self):
        from wx_bot import Bot
        bot = Bot(Config(), db=None, dry_run=True)
        bot.sessions.touch_profile("wxid_a", name="张三", wxid="wxid_a")
        bot.sessions.bump_affinity("wxid_a", +5, "被夸")
        sent = []
        bot.send = lambda chat_id, text: sent.append(text) or True
        bot._send_affinity("wxid_a", "wxid_a")
        self.assertEqual(sent, ["好感度 55/100（眼熟）｜今天 +5"])

    def test_query_command_for_unknown_person_uses_default(self):
        from wx_bot import Bot
        bot = Bot(Config(), db=None, dry_run=True)
        sent = []
        bot.send = lambda chat_id, text: sent.append(text) or True
        bot._send_affinity("wxid_new", "wxid_new")
        self.assertEqual(sent, ["好感度 50/100（眼熟）"])

    def test_query_command_ignores_stale_daily_quota(self):
        from wx_bot import Bot
        bot = Bot(Config(), db=None, dry_run=True)
        bot.sessions.touch_profile("wxid_a")
        p = bot.sessions._profiles["wxid_a"]
        p["affinity"], p["affinity_day"] = 62, "1999-01-01"
        p["affinity_up"], p["affinity_down"] = 9, 3
        sent = []
        bot.send = lambda chat_id, text: sent.append(text) or True
        bot._send_affinity("wxid_a", "wxid_a")
        self.assertEqual(sent, ["好感度 62/100（熟络）"], "昨天的额度不能算今天")

    def test_extract_affinity(self):
        from brain import extract_affinity
        text, hits = extract_affinity("好开心呀\n[好感:+3 被夸]")
        self.assertEqual(text, "好开心呀")
        self.assertEqual(hits, [(3, "被夸")])

    def test_extract_affinity_negative_and_multiple(self):
        from brain import extract_affinity
        text, hits = extract_affinity("[好感:-2 被骂]\n好吧\n[好感:+1]")
        self.assertEqual(text, "好吧")
        self.assertEqual(hits, [(-2, "被骂"), (1, "")])

    def test_extract_affinity_plain_text(self):
        from brain import extract_affinity
        self.assertEqual(extract_affinity("今天好累"), ("今天好累", []))


class TestMoments(unittest.TestCase):
    """瞬间记忆：她自己的感受，区别于 traits（事实）。"""

    def _st(self, **kw):
        return SessionStore(Config(**kw))

    def test_add_and_dedupe(self):
        st = self._st()
        st.touch_profile(FRIEND, name="阿鱼")
        self.assertTrue(st.add_moment(FRIEND, "他第一次叫我小鲸鱼"))
        self.assertFalse(st.add_moment(FRIEND, "他第一次叫我小鲸鱼"), "同样的不重复记")
        self.assertEqual(len(st.profile(FRIEND)["moments"]), 1)

    def test_empty_text_ignored(self):
        st = self._st()
        st.touch_profile(FRIEND)
        self.assertFalse(st.add_moment(FRIEND, "   "))

    def test_capped_keeps_newest(self):
        st = self._st(memory_moments=3)
        st.touch_profile(FRIEND)
        for i in range(5):
            st.add_moment(FRIEND, f"瞬间{i}")
        texts = [m["text"] for m in st.profile(FRIEND)["moments"]]
        self.assertEqual(texts, ["瞬间2", "瞬间3", "瞬间4"])

    def test_length_truncated(self):
        st = self._st()
        st.touch_profile(FRIEND)
        st.add_moment(FRIEND, "啊" * 200)
        self.assertLessEqual(len(st.profile(FRIEND)["moments"][0]["text"]), 60)

    def test_injected_into_prompt(self):
        st = self._st()
        st.touch_profile(FRIEND, name="阿鱼")
        st.add_moment(FRIEND, "凌晨三点他还陪我聊天")
        text = st.profile_text(FRIEND)
        self.assertIn("你记得的几个瞬间", text)
        self.assertIn("凌晨三点他还陪我聊天", text)

    def test_only_recent_shown(self):
        st = self._st(memory_moments=10)
        st.touch_profile(FRIEND, name="阿鱼")
        for i in range(6):
            st.add_moment(FRIEND, f"第{i}个瞬间")
        text = st.profile_text(FRIEND)
        self.assertIn("第5个瞬间", text)
        self.assertNotIn("第0个瞬间", text, "只带最近几条，控制 token")

    def test_old_profile_defaults_empty(self):
        st = self._st()
        st.touch_profile(FRIEND)
        self.assertEqual(st.profile(FRIEND)["moments"], [])

    def test_reset_keeps_moments(self):
        st = self._st()
        st.touch_profile(FRIEND)
        st.add_moment(FRIEND, "某个瞬间")
        st.append(FRIEND, "q", "a")
        st.reset(FRIEND)
        self.assertEqual(len(st.profile(FRIEND)["moments"]), 1)

    def test_extract_moments(self):
        from brain import extract_moments
        text, ms = extract_moments("诶嘿\n[回忆:他第一次叫我小鲸鱼]")
        self.assertEqual(text, "诶嘿")
        self.assertEqual(ms, ["他第一次叫我小鲸鱼"])

    def test_plain_text_has_no_moments(self):
        from brain import extract_moments
        self.assertEqual(extract_moments("今天好累"), ("今天好累", []))


class TestKaomoji(unittest.TestCase):
    """颜文字：模型标情绪，程序挑字 + 控频率。"""

    def _bot(self, **kw):
        from wx_bot import Bot
        return Bot(Config(**kw), db=_FakeDB2(), dry_run=True)

    def test_library_covers_moods(self):
        from brain import KAOMOJI
        for mood in ("开心", "害羞", "委屈", "生气", "困", "疑惑", "得意", "无语", "撒娇", "惊讶"):
            self.assertIn(mood, KAOMOJI)
            self.assertTrue(all(len(f) <= 16 for f in KAOMOJI[mood]), mood)

    def test_extract_kaomoji(self):
        from brain import extract_kaomoji
        self.assertEqual(extract_kaomoji("好呀\n[颜:害羞]"), ("好呀", "害羞"))

    def test_plain_text_has_no_mood(self):
        from brain import extract_kaomoji
        self.assertEqual(extract_kaomoji("今天好累"), ("今天好累", ""))

    def test_pick_from_mood(self):
        from brain import KAOMOJI, pick_kaomoji
        self.assertIn(pick_kaomoji("开心"), KAOMOJI["开心"])
        self.assertEqual(pick_kaomoji("发疯"), "", "不认识的情绪宁可不加")

    def test_append_to_last_line(self):
        bot = self._bot(kaomoji_rate=1.0, kaomoji_cooldown=0)
        out = bot._maybe_kaomoji("c1", "第一行\n第二行", "开心")
        self.assertTrue(out.startswith("第一行\n第二行 "))
        self.assertNotEqual(out, "第一行\n第二行")

    def test_cooldown_blocks_second(self):
        bot = self._bot(kaomoji_rate=1.0, kaomoji_cooldown=600)
        first = bot._maybe_kaomoji("c1", "在发呆", "开心")
        self.assertNotEqual(first, "在发呆")
        self.assertEqual(bot._maybe_kaomoji("c1", "在发呆", "开心"), "在发呆", "冷却内不贴")

    def test_other_chat_not_blocked(self):
        bot = self._bot(kaomoji_rate=1.0, kaomoji_cooldown=600)
        bot._maybe_kaomoji("c1", "在发呆", "开心")
        self.assertNotEqual(bot._maybe_kaomoji("c2", "在发呆", "困"), "在发呆")

    def test_disabled(self):
        bot = self._bot(kaomoji_enabled=False, kaomoji_rate=1.0, kaomoji_cooldown=0)
        self.assertEqual(bot._maybe_kaomoji("c1", "在发呆", "开心"), "在发呆")

    def test_empty_text_untouched(self):
        bot = self._bot(kaomoji_rate=1.0, kaomoji_cooldown=0)
        self.assertEqual(bot._maybe_kaomoji("c1", "", "开心"), "")

    def test_unknown_mood_no_change(self):
        bot = self._bot(kaomoji_rate=1.0, kaomoji_cooldown=0)
        self.assertEqual(bot._maybe_kaomoji("c1", "在发呆", "发疯"), "在发呆")


class TestBotWiring(unittest.TestCase):
    """静态检查 wx_bot.py 的导入，防住踩过的坑。"""

    def test_listener_imported_from_db_module(self):
        # wechatauto 根包的 Listener 是抽象基类，接参数会 TypeError；
        # 真正的监听器在 wechatauto.db 里。
        import ast
        import pathlib

        src = pathlib.Path(__file__).with_name("wx_bot.py").read_text("utf-8")
        imports = {}
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[alias.asname or alias.name] = node.module

        self.assertEqual(imports.get("Listener"), "wechatauto.db")
        self.assertEqual(imports.get("WeChatDB"), "wechatauto.db")


class TestMaintenance(unittest.TestCase):
    """日志轮转、识图临时文件清理、dry-run 不写真实记忆。

    懒创建目录：沙箱里 tempfile 不一定能用，所以用项目内的固定目录，用完就删。
    """

    def setUp(self):
        self.dir = pathlib.Path(__file__).parent / "_test_maintenance"
        self.dir.mkdir(exist_ok=True)
        for p in self.dir.glob("*"):
            if p.is_file():
                p.unlink()

    def tearDown(self):
        for p in self.dir.glob("*"):
            if p.is_file():
                p.unlink()
        self.dir.rmdir()

    def _file(self, name: str, age_days: float = 0.0) -> pathlib.Path:
        p = self.dir / name
        p.write_bytes(b"x")
        ts = time.time() - age_days * 86400
        os.utime(p, (ts, ts))
        return p

    def test_prune_by_age(self):
        from wx_bot import prune_images

        old = self._file("old.jpg", 10)
        fresh = self._file("fresh.jpg", 1)
        self.assertEqual(prune_images(str(self.dir), keep_days=7, keep_max=200), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_prune_by_count_keeps_newest(self):
        from wx_bot import prune_images

        for i in range(10):        # img0 最新 → img9 最旧
            self._file(f"img{i}.jpg", 2 + i)
        self.assertEqual(prune_images(str(self.dir), keep_days=30, keep_max=4), 6)
        left = sorted(p.name for p in self.dir.glob("*"))
        self.assertEqual(left, ["img0.jpg", "img1.jpg", "img2.jpg", "img3.jpg"])

    def test_prune_never_deletes_just_downloaded(self):
        from wx_bot import prune_images

        for i in range(3):
            self._file(f"new{i}.jpg")          # mtime = 现在
        for i in range(3):
            self._file(f"old{i}.jpg", 5)
        # 上限 2 张，但新的 3 张刚落地（可能在编码），只能删旧的 3 张
        self.assertEqual(prune_images(str(self.dir), keep_days=30, keep_max=2), 3)
        self.assertEqual(len(list(self.dir.glob("*"))), 3)

    def test_prune_missing_dir(self):
        from wx_bot import prune_images

        self.assertEqual(prune_images(str(self.dir / "nope")), 0)

    def test_prune_keep_max_zero_means_unlimited(self):
        from wx_bot import prune_images

        for i in range(5):
            self._file(f"img{i}.jpg", 2)
        self.assertEqual(prune_images(str(self.dir), keep_days=30, keep_max=0), 0)
        self.assertEqual(len(list(self.dir.glob("*"))), 5)

    def test_log_uses_rotating_handler(self):
        import logging
        from logging.handlers import RotatingFileHandler

        from wx_bot import setup_logging

        cfg = Config(log_file=str(self.dir / "t.log"), log_max_bytes=4096, log_backups=3)
        logger = logging.getLogger("wxbot")
        before = list(logger.handlers)
        try:
            setup_logging(cfg)
            rotating = [h for h in logger.handlers
                        if isinstance(h, RotatingFileHandler)]
            self.assertEqual(len(rotating), 1)
            self.assertEqual(rotating[0].maxBytes, 4096)
            self.assertEqual(rotating[0].backupCount, 3)
            # 直接往文件 handler 灌大记录，验证真的会轮转（不吵控制台）
            fh = rotating[0]
            for _ in range(20):
                fh.emit(logging.LogRecord(
                    "wxbot", logging.INFO, __file__, 1, "y" * 2000, None, None))
            fh.flush()
            names = [p.name for p in self.dir.glob("t.log*")]
            self.assertIn("t.log", names)
            self.assertTrue(any(n != "t.log" for n in names), names)
        finally:
            for h in list(logger.handlers):
                if h not in before:
                    h.close()
                    logger.removeHandler(h)

    def test_log_rotation_can_be_disabled(self):
        import logging

        from wx_bot import setup_logging

        cfg = Config(log_file=str(self.dir / "t.log"), log_max_bytes=0)
        logger = logging.getLogger("wxbot")
        before = list(logger.handlers)
        try:
            setup_logging(cfg)
            files = [h for h in logger.handlers
                     if isinstance(h, logging.FileHandler)]
            self.assertEqual(len(files), 1)
            self.assertFalse(hasattr(files[0], "backupCount"))
        finally:
            for h in list(logger.handlers):
                if h not in before:
                    h.close()
                    logger.removeHandler(h)

    def test_dry_run_keeps_memory_in_memory(self):
        from wx_bot import Bot

        mem = self.dir / "mem.json"
        cfg = Config(memory_file=str(mem), system_prompt="（测试）")
        bot = Bot(cfg, db=None, dry_run=True)
        self.assertEqual(bot.sessions.path, "")
        bot.sessions.touch_profile("wxid_x", "小明", "wxid_x")
        bot.sessions.flush()
        self.assertFalse(mem.exists())

    def test_normal_run_persists_memory(self):
        from wx_bot import Bot

        mem = self.dir / "mem.json"
        cfg = Config(memory_file=str(mem), system_prompt="（测试）")
        bot = Bot(cfg, db=None, dry_run=False)
        self.assertEqual(bot.sessions.path, str(mem))
        bot.sessions.touch_profile("wxid_x", "小明", "wxid_x")
        bot.sessions.flush()
        self.assertTrue(mem.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
