import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
MAIN_PATH = PLUGIN_DIR / "main.py"


def _install_astrbot_stubs(data_dir_holder):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")

    class StubFilter:
        class PermissionType:
            ADMIN = "admin"

        @staticmethod
        def command(*args, **kwargs):
            def decorate(func):
                return func

            return decorate

        @staticmethod
        def permission_type(*args, **kwargs):
            def decorate(func):
                return func

            return decorate

    class StubStar:
        def __init__(self, context):
            self.context = context

    class StubStarTools:
        @staticmethod
        def get_data_dir(_plugin_name):
            return str(data_dir_holder["path"])

    class StubLogger:
        @staticmethod
        def info(*_args, **_kwargs):
            pass

        @staticmethod
        def error(*_args, **_kwargs):
            pass

    def register(*_args, **_kwargs):
        def decorate(cls):
            return cls

        return decorate

    api.AstrBotConfig = dict
    api.logger = StubLogger()
    event_module.AstrMessageEvent = object
    event_module.filter = StubFilter
    star_module.Context = object
    star_module.Star = StubStar
    star_module.StarTools = StubStarTools
    star_module.register = register

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module


class FakeEvent:
    def __init__(self, sender_id="7001", admin=False):
        self.sender_id = sender_id
        self.admin = admin

    def get_sender_id(self):
        return self.sender_id

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text


def _collect(async_generator):
    async def collect():
        return [item async for item in async_generator]

    return asyncio.run(collect())


class XingxyPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = tempfile.TemporaryDirectory()
        cls.data_dir_holder = {"path": Path(cls.temp_root.name) / "astrbot-data"}
        _install_astrbot_stubs(cls.data_dir_holder)
        spec = importlib.util.spec_from_file_location("xingxy_test_plugin_main", MAIN_PATH)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.temp_root.cleanup()
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star"):
            sys.modules.pop(name, None)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir_holder["path"] = Path(self.temp_dir.name) / "plugin-data"
        self.plugin = self.module.XingxyPlugin(context=None, config={})

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_startup_creates_only_plugin_data_directory(self):
        self.assertTrue(self.plugin.data_dir.is_dir())
        self.assertFalse(self.plugin.store.path.exists())

    def test_identity_command_uses_sender_and_initializes_synthetic_binding(self):
        output = _collect(self.plugin.cmd_identity(FakeEvent("7001")))[0]
        self.assertIn("TG UID：7001", output)
        self.assertIn("绑定状态：active", output)
        self.assertIn("测试 user_id：10001", output)
        self.assertIn("积分余额：0", output)

    def test_checkin_is_persisted_and_same_event_replays_without_award(self):
        first = _collect(self.plugin.cmd_checkin(FakeEvent(), "evt-1"))[0]
        self.assertIn("签到成功：+10 积分", first)

        reloaded = self.module.XingxyPlugin(context=None, config={})
        replay = _collect(reloaded.cmd_checkin(FakeEvent(), "evt-1"))[0]
        self.assertIn("replay", replay)
        state = reloaded.store.load()
        self.assertEqual(state["points"]["10001"], 10)
        self.assertEqual(len(state["checkin_events"]), 1)

    def test_different_event_same_shanghai_day_is_already_checked_in(self):
        first = self.module.JsonTestStore(self.plugin.store.path)
        result1, _ = first.checkin("7001", "evt-first", 7, True, 10001, today="2026-11-08")
        result2, event2 = first.checkin("7001", "evt-second", 7, True, 10001, today="2026-11-08")
        self.assertEqual(result1, "success")
        self.assertEqual(result2, "already_checked_in")
        self.assertEqual(event2["first_event_id"], "evt-first")
        self.assertEqual(first.load()["points"]["10001"], 7)

    def test_checkin_command_reports_already_checked_in_for_a_second_event(self):
        _collect(self.plugin.cmd_checkin(FakeEvent(), "evt-command-first"))
        output = _collect(self.plugin.cmd_checkin(FakeEvent(), "evt-command-second"))[0]
        self.assertIn("already_checked_in", output)
        self.assertIn("evt-command-first", output)
        self.assertEqual(self.plugin.store.load()["points"]["10001"], 10)

    def test_new_shanghai_day_allows_one_more_reward_and_configurable_amount(self):
        store = self.module.JsonTestStore(self.plugin.store.path)
        store.checkin("7001", "evt-day-1", 5, True, 10001, today="2026-11-08")
        result, _ = store.checkin("7001", "evt-day-2", 5, True, 10001, today="2026-11-09")
        self.assertEqual(result, "success")
        self.assertEqual(store.load()["points"]["10001"], 10)

    def test_shanghai_date_uses_local_calendar_day(self):
        utc_time = datetime(2026, 11, 7, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(self.module.shanghai_date(utc_time), "2026-11-08")

    def test_reset_requires_admin_by_default_and_clears_persisted_data(self):
        _collect(self.plugin.cmd_checkin(FakeEvent(), "evt-reset"))
        denied = _collect(self.plugin.cmd_reset(FakeEvent(admin=False)))[0]
        self.assertIn("仅允许管理员", denied)
        self.assertEqual(len(self.plugin.store.load()["checkin_events"]), 1)

        accepted = _collect(self.plugin.cmd_reset(FakeEvent(admin=True)))[0]
        self.assertIn("已清空", accepted)
        state = self.plugin.store.load()
        self.assertEqual(state["identities"], {})
        self.assertEqual(state["points"], {})
        self.assertEqual(state["checkin_events"], {})

    def test_flow_shows_required_pipeline_and_summary(self):
        output = _collect(self.plugin.cmd_flow(FakeEvent("7777")))[0]
        for expected in (
            "消息：",
            "身份解析：",
            "事件幂等：",
            "签到日：Asia/Shanghai",
            "积分：",
            "响应：",
            "当前数据摘要：",
        ):
            self.assertIn(expected, output)

    def test_unbound_identity_does_not_award_points(self):
        plugin = self.module.XingxyPlugin(
            context=None,
            config={"default_bound": False, "reward_points": 10},
        )
        output = _collect(plugin.cmd_checkin(FakeEvent(), "evt-unbound"))[0]
        self.assertIn("未绑定", output)
        self.assertEqual(plugin.store.load()["points"], {})


if __name__ == "__main__":
    unittest.main()
