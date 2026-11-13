import asyncio
import importlib.util
import json
import logging
import queue
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from logging.handlers import QueueHandler
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode


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

        @staticmethod
        def regex(*args, **kwargs):
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

        @staticmethod
        def warning(*_args, **_kwargs):
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
    def __init__(
        self,
        sender_id="7001",
        admin=False,
        platform="telegram",
        message_type="FriendMessage",
        group_id="",
        message="",
    ):
        self.sender_id = sender_id
        self.admin = admin
        self.platform = platform
        self.message_type = message_type
        self.group_id = group_id
        self.message = message
        self.stopped = False

    def get_sender_id(self):
        return self.sender_id

    def is_admin(self):
        return self.admin

    def get_platform_name(self):
        return self.platform

    def get_message_type(self):
        return self.message_type

    def get_group_id(self):
        return self.group_id

    def get_message_str(self):
        return self.message

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class FakeContext:
    def __init__(self, trace_enable=False):
        self.config = {"trace_enable": trace_enable}
        self.config_reads = []

    def get_config(self, umo=None):
        self.config_reads.append(umo)
        return self.config


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
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        spec = importlib.util.spec_from_file_location("astrbot_plugin_xingxy.main", MAIN_PATH)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.temp_root.cleanup()
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star"):
            sys.modules.pop(name, None)
        for name in ("astrbot_plugin_xingxy.main", "astrbot_plugin_xingxy.site_api", "astrbot_plugin_xingxy.service_signature"):
            sys.modules.pop(name, None)
        sys.modules.pop("astrbot_plugin_xingxy", None)
        try:
            sys.path.remove(str(PLUGIN_DIR.parent))
        except ValueError:
            pass

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir_holder["path"] = Path(self.temp_dir.name) / "plugin-data"
        self.plugin = self.module.XingxyPlugin(context=None, config={})

    def tearDown(self):
        asyncio.run(self.plugin.terminate())
        self.temp_dir.cleanup()

    def test_startup_creates_only_plugin_data_directory(self):
        self.assertTrue(self.plugin.data_dir.is_dir())
        self.assertFalse(self.plugin.store.path.exists())

    def test_identity_command_uses_sender_and_initializes_synthetic_binding(self):
        output = _collect(self.plugin.cmd_identity(FakeEvent("7001")))[0]
        self.assertIn("数据来源：AstrBot 本地模拟", output)
        self.assertIn("TG UID：7001", output)
        self.assertIn("绑定状态：active", output)
        self.assertIn("测试 user_id：10001", output)
        self.assertIn("积分余额：0", output)

    def test_checkin_is_persisted_and_same_event_replays_without_award(self):
        first = _collect(self.plugin.cmd_checkin(FakeEvent(), "evt-1"))[0]
        self.assertIn("数据来源：AstrBot 本地模拟", first)
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
            "数据来源：AstrBot 本地模拟",
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

    def test_site_checkin_client_signs_exact_route_payload_and_replay(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        captured = []
        response = b'{"ok":true,"checked_in":true,"date":"2026-11-08","amount":10}'

        def transport(url, body, headers):
            captured.append((url, body, headers))
            return 200, response

        client = site_api.SiteApiClient(
            {
                "site_base_url": "https://example.test/",
                "service_key_id": "checkin-key",
                "service_secret": "c" * 32,
            },
            transport=transport,
        )
        with patch(
            "astrbot_plugin_xingxy.site_api.service_signature.sign",
            return_value="a" * 64,
        ) as signer:
            first = asyncio.run(client.checkin("7001", "request-uuid-1"))
            replay = asyncio.run(client.checkin("7001", "request-uuid-1"))

        self.assertEqual(first, {"outcome": "success", "date": "2026-11-08", "amount": 10})
        self.assertEqual(replay, first)
        self.assertEqual(len(captured), 2)
        for url, body, headers in captured:
            self.assertEqual(
                url,
                "https://example.test/wp-json/xingxy-ecosystem/v1/tasks/checkin",
            )
            self.assertEqual(
                body,
                b'{"event_id":"request-uuid-1","tg_uid":"7001"}',
            )
            self.assertEqual(headers["X-Xingxy-Key-Id"], "checkin-key")
            self.assertEqual(headers["X-Xingxy-Signature"], "a" * 64)
        self.assertEqual(signer.call_count, 2)
        args = signer.call_args.args
        self.assertEqual(args[0], b"c" * 32)
        self.assertEqual(args[1], "checkin-key")
        self.assertEqual(
            args[4:7],
            ("POST", site_api.CHECKIN_ROUTE, b'{"event_id":"request-uuid-1","tg_uid":"7001"}'),
        )

    def test_site_checkin_client_validates_already_checked_and_generic_forbidden(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        client = site_api.SiteApiClient(
            {"site_base_url": "https://example.test", "service_key_id": "checkin",
             "service_secret": "c" * 32},
            transport=lambda *_args: (
                409,
                b'{"ok":false,"checked_in":false,"reason":"already_checked_in",'
                b'"date":"2026-11-08","amount":0}',
            ),
        )
        self.assertEqual(
            asyncio.run(client.checkin("7001", "stable-id")),
            {"outcome": "already_checked_in", "date": "2026-11-08", "amount": 0},
        )
        client.transport = lambda *_args: (403, b'{"detail":"private allowlist data"}')
        self.assertEqual(
            asyncio.run(client.checkin("7001", "stable-id")),
            {"outcome": "unbound"},
        )

    def test_site_checkin_client_accepts_optional_native_experience_and_preserves_points_only_shape(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        client = site_api.SiteApiClient(
            {"site_base_url": "https://example.test", "service_key_id": "checkin",
             "service_secret": "c" * 32},
            transport=lambda *_args: (
                200,
                b'{"ok":true,"checked_in":true,"date":"2026-11-08",'
                b'"amount":35,"integral":40}',
            ),
        )
        self.assertEqual(
            asyncio.run(client.checkin("7001", "xp-success")),
            {"outcome": "success", "date": "2026-11-08", "amount": 35, "integral": 40},
        )

        client.transport = lambda *_args: (
            200,
            b'{"ok":true,"checked_in":true,"date":"2026-11-08","amount":35}',
        )
        self.assertEqual(
            asyncio.run(client.checkin("7001", "points-only")),
            {"outcome": "success", "date": "2026-11-08", "amount": 35},
        )

        for value in (0, 2147483647):
            with self.subTest(boundary=value):
                body = {"ok": True, "checked_in": True, "date": "2026-11-08",
                        "amount": 0, "integral": value}
                client.transport = lambda *_args, body=body: (200, json.dumps(body).encode())
                self.assertEqual(
                    asyncio.run(client.checkin("7001", "xp-boundary")),
                    {"outcome": "success", "date": "2026-11-08", "amount": 0, "integral": value},
                )

    def test_site_checkin_client_rejects_malformed_optional_experience(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        client = site_api.SiteApiClient(
            {"site_base_url": "https://example.test", "service_key_id": "checkin",
             "service_secret": "c" * 32},
        )
        base = {"ok": True, "checked_in": True, "date": "2026-11-08", "amount": 35}
        for value in (True, False, -1, 2147483648, "40", 40.0, None):
            with self.subTest(value=value):
                body = dict(base, integral=value)
                client.transport = lambda *_args, body=body: (200, json.dumps(body).encode())
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.checkin("7001", "bad-xp"))

        conflict = {
            "ok": False,
            "checked_in": False,
            "reason": "already_checked_in",
            "date": "2026-11-08",
            "amount": 0,
        }
        for value in (0, 40, 2147483647, True, -1, 2147483648, "0"):
            with self.subTest(conflict_value=value):
                body = dict(conflict, integral=value)
                client.transport = lambda *_args, body=body: (409, json.dumps(body).encode())
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.checkin("7001", "bad-conflict-xp"))

        for status, base_body in ((200, base), (409, conflict)):
            body = dict(base_body, experience_total=1000)
            with self.subTest(status=status, body=body):
                client.transport = lambda *_args, status=status, body=body: (status, json.dumps(body).encode())
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.checkin("7001", "unexpected-xp-fields"))
        for status, base_body in ((200, base), (409, conflict)):
            body = {key: value for key, value in base_body.items() if key != "amount"}
            with self.subTest(status=status, body=body):
                client.transport = lambda *_args, status=status, body=body: (status, json.dumps(body).encode())
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.checkin("7001", "missing-points"))

    def test_site_checkin_conflict_rejects_any_experience_field(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        client = site_api.SiteApiClient(
            {"site_base_url": "https://example.test", "service_key_id": "checkin",
             "service_secret": "c" * 32},
        )
        conflict = {
            "ok": False,
            "checked_in": False,
            "reason": "already_checked_in",
            "date": "2026-11-08",
            "amount": 0,
        }
        for value in (0, 40, 2147483647, True, -1, 2147483648, "40", None):
            with self.subTest(integral=value):
                body = dict(conflict, integral=value)
                client.transport = lambda *_args, body=body: (409, json.dumps(body).encode())
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.checkin("7001", "conflict-with-xp"))

    def test_site_checkin_text_reports_positive_experience_without_fabricating_total(self):
        config = {
            "backend_mode": "site",
            "site_checkin_enabled": True,
            "site_base_url": "https://example.test",
            "checkin_service_key_id": "checkin-key",
            "checkin_service_secret": "c" * 32,
        }
        plugin = self.module.XingxyPlugin(context=FakeContext(), config=config)
        responses = iter(
            (
                (200, b'{"ok":true,"checked_in":true,"date":"2026-11-08",'
                 b'"amount":35,"integral":40}'),
                (200, b'{"ok":true,"checked_in":true,"date":"2026-11-08",'
                 b'"amount":35,"integral":40}'),
                (200, b'{"ok":true,"checked_in":true,"date":"2026-11-08",'
                 b'"amount":35,"integral":0}'),
            )
        )
        calls = []

        def transport(url, body, headers):
            calls.append(body)
            return next(responses)

        plugin.site_transport = transport
        first = _collect(plugin.cmd_checkin(FakeEvent("7001"), "frozen-event"))[0]
        retry = _collect(plugin.cmd_checkin(FakeEvent("7001"), "frozen-event"))[0]
        zero = _collect(plugin.cmd_checkin(FakeEvent("7001"), "zero-xp"))[0]
        self.assertIn("+35 积分，+40 经验", first)
        self.assertNotIn("余额", first)
        self.assertNotIn("经验总", first)
        self.assertEqual(retry, first)
        self.assertIn("+35 积分", zero)
        self.assertNotIn("经验", zero)
        self.assertEqual(calls[0], calls[1])
        self.assertFalse(plugin.store.path.exists())

    def test_site_checkin_experience_timeout_retry_keeps_event_and_frozen_site_result(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(),
            config={"backend_mode": "site", "site_checkin_enabled": True,
                    "site_base_url": "https://example.test",
                    "checkin_service_key_id": "checkin-key",
                    "checkin_service_secret": "c" * 32,
                    "reward_points": 999},
        )
        saved = {}
        requests = []
        current_reply = {"ok": True, "checked_in": True, "date": "2026-11-08",
                         "amount": 35, "integral": 40}

        def transport(url, body, headers):
            request = json.loads(body)
            requests.append(request)
            event_id = request["event_id"]
            if event_id not in saved:
                # Simulate a successful server commit whose response was lost.
                saved[event_id] = dict(current_reply)
                raise TimeoutError("private lost response")
            return 200, json.dumps(saved[event_id]).encode()

        plugin.site_transport = transport
        with patch.object(self.module.uuid, "uuid4", return_value="stable-generated-xp") as factory:
            failure = _collect(plugin.cmd_checkin(FakeEvent("7001")))[0]
            self.assertIn("/xy_checkin stable-generated-xp", failure)
            self.assertNotIn("+40 经验", failure)
            self.assertNotIn("private lost response", failure)
            current_reply.update(date="2026-11-09", amount=60, integral=80)
            plugin.config["reward_points"] = 1234
            retry = _collect(plugin.cmd_checkin(FakeEvent("7001"), "stable-generated-xp"))[0]
            repeated = _collect(plugin.cmd_checkin(FakeEvent("7001"), "stable-generated-xp"))[0]
        factory.assert_called_once_with()
        self.assertEqual(retry, repeated)
        self.assertIn("+35 积分，+40 经验", retry)
        self.assertIn("2026-11-08", retry)
        self.assertNotIn("2026-11-09", retry)
        self.assertNotIn("1234", retry)
        self.assertEqual(requests, [{"event_id": "stable-generated-xp", "tg_uid": "7001"}] * 3)
        self.assertEqual(len(saved), 1)
        self.assertFalse(plugin.store.path.exists())

    def test_site_checkin_command_uses_dedicated_secret_and_never_local_data(self):
        config = {
            "backend_mode": "site",
            "site_checkin_enabled": True,
            "site_base_url": "https://example.test",
            "service_key_id": "identity-key",
            "service_secret": "i" * 32,
            "points_service_key_id": "points-key",
            "points_service_secret": "p" * 32,
            "binding_service_key_id": "binding-key",
            "binding_service_secret": "b" * 32,
            "checkin_service_key_id": "checkin-key",
            "checkin_service_secret": "c" * 32,
        }
        plugin = self.module.XingxyPlugin(context=FakeContext(), config=config)
        calls = []

        def transport(url, body, headers):
            calls.append((url, body, headers))
            return 200, b'{"ok":true,"checked_in":true,"date":"2026-11-08","amount":12}'

        plugin.site_transport = transport
        output = _collect(plugin.cmd_checkin(FakeEvent("7001"), "stable-id"))[0]
        self.assertIn("网站签到成功：+12 积分", output)
        self.assertIn("event_id=stable-id", output)
        self.assertEqual(len(calls), 1)
        url, body, headers = calls[0]
        self.assertTrue(url.endswith("/tasks/checkin"))
        self.assertEqual(body, b'{"event_id":"stable-id","tg_uid":"7001"}')
        self.assertEqual(headers["X-Xingxy-Key-Id"], "checkin-key")
        signature_module = sys.modules["astrbot_plugin_xingxy.service_signature"]
        self.assertEqual(
            headers["X-Xingxy-Signature"],
            signature_module.sign(
                b"c" * 32, "checkin-key", headers["X-Xingxy-Timestamp"],
                headers["X-Xingxy-Nonce"], "POST",
                "/xingxy-ecosystem/v1/tasks/checkin", body,
            ),
        )
        self.assertFalse(plugin.store.path.exists())

    def test_site_checkin_generates_uuid_and_maps_403_without_echoing_body(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(),
            config={"backend_mode": "site", "site_checkin_enabled": True,
                    "site_base_url": "https://example.test",
                    "checkin_service_key_id": "checkin-key",
                    "checkin_service_secret": "c" * 32},
        )
        calls = []

        def forbidden(url, body, headers):
            calls.append(body)
            return 403, b"secret allowlist explanation"

        plugin.site_transport = forbidden
        class FixedUuid:
            def __str__(self):
                return "12345678-1234-5678-1234-567812345678"

        with patch.object(self.module.uuid, "uuid4") as uuid_factory:
            uuid_factory.return_value = FixedUuid()
            output = _collect(plugin.cmd_checkin(FakeEvent("7001")))[0]
        self.assertIn("未绑定站点，或暂时无权签到", output)
        self.assertNotIn("secret allowlist", output)
        self.assertEqual(
            calls,
            [b'{"event_id":"12345678-1234-5678-1234-567812345678","tg_uid":"7001"}'],
        )
        self.assertFalse(plugin.store.path.exists())

    def test_site_checkin_is_disabled_for_local_backend_group_other_platform_and_trace(self):
        config = {
            "backend_mode": "site",
            "site_checkin_enabled": True,
            "site_base_url": "https://example.test",
            "checkin_service_key_id": "checkin-key",
            "checkin_service_secret": "c" * 32,
        }
        calls = []
        plugin = self.module.XingxyPlugin(context=FakeContext(), config=config)
        plugin.site_transport = lambda *args: calls.append(args)
        disabled = self.module.XingxyPlugin(
            context=FakeContext(), config=dict(config, site_checkin_enabled=False)
        )
        disabled.site_transport = plugin.site_transport
        self.assertIn("未启用", _collect(disabled.cmd_checkin(FakeEvent()))[0])

        local = self.module.XingxyPlugin(
            context=FakeContext(), config=dict(config, backend_mode="local")
        )
        local.site_transport = plugin.site_transport
        local_output = _collect(local.cmd_checkin(FakeEvent(), "local-event"))[0]
        self.assertIn("AstrBot 本地模拟", local_output)
        self.assertEqual(calls, [])
        before = plugin.store.path.read_bytes()

        for event in (FakeEvent(group_id="-100123"), FakeEvent(platform="discord")):
            output = _collect(plugin.cmd_checkin(event, "blocked-event"))[0]
            self.assertIn("仅接受 Telegram 私聊", output)
        traced = self.module.XingxyPlugin(context=FakeContext(trace_enable=True), config=config)
        traced.site_transport = plugin.site_transport
        output = _collect(traced.cmd_checkin(FakeEvent(), "trace-event"))[0]
        self.assertIn("关闭 AstrBot 全局 trace_enable", output)
        self.assertEqual(calls, [])
        self.assertEqual(plugin.store.path.read_bytes(), before)

    def test_site_checkin_malformed_and_http_failure_do_not_fall_back_or_change_test_data(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(),
            config={"backend_mode": "site", "site_checkin_enabled": True,
                    "site_base_url": "https://example.test",
                    "checkin_service_key_id": "checkin-key",
                    "checkin_service_secret": "c" * 32},
        )
        plugin.store.checkin("7001", "synthetic-event", 10, True, 10001)
        before = plugin.store.path.read_bytes()
        cases = (
            (200, b'{"ok":true,"checked_in":true,"date":"2026-02-30","amount":10}'),
            (200, b'{"ok":true,"checked_in":true,"date":"2026-11-08","amount":true}'),
            (409, b'{"ok":false,"checked_in":false,"reason":"already_checked_in",'
                  b'"date":"2026-11-08","amount":1}'),
            (503, b"private upstream failure"),
        )
        for index, response in enumerate(cases):
            with self.subTest(response=response[0]):
                plugin.site_transport = lambda *_args, response=response: response
                output = _collect(plugin.cmd_checkin(FakeEvent("7001"), "retry-stable"))[0]
                self.assertIn("网站签到结果暂时无法确认", output)
                self.assertIn("/xy_checkin retry-stable", output)
                self.assertNotIn("测试 user_id", output)
                self.assertNotIn("private upstream", output)
                self.assertEqual(plugin.store.path.read_bytes(), before)

    def test_site_checkin_timeout_returns_generated_event_id_for_exact_retry(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(),
            config={"backend_mode": "site", "site_checkin_enabled": True,
                    "site_base_url": "https://example.test",
                    "checkin_service_key_id": "checkin-key",
                    "checkin_service_secret": "c" * 32},
        )
        captured = {}
        def transport(url, body, headers):
            captured.update(json.loads(body))
            raise TimeoutError("private upstream diagnostic")
        plugin.site_transport = transport
        output = _collect(plugin.cmd_checkin(FakeEvent("7001")))[0]
        self.assertIn("/xy_checkin " + captured["event_id"], output)
        self.assertNotIn("private upstream", output)
        self.assertFalse(plugin.store.path.exists())

    def test_site_checkin_missing_tasks_key_never_uses_other_credentials(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(),
            config={"backend_mode": "site", "site_checkin_enabled": True,
                    "site_base_url": "https://example.test",
                    "service_key_id": "identity-key", "service_secret": "i" * 32,
                    "binding_service_key_id": "binding-key", "binding_service_secret": "b" * 32,
                    "points_service_key_id": "points-key", "points_service_secret": "p" * 32},
        )
        calls = []
        plugin.site_transport = lambda *args: calls.append(args)
        output = _collect(plugin.cmd_checkin(FakeEvent("7001"), "missing-key"))[0]
        self.assertIn("签到专用凭据未就绪", output)
        self.assertEqual(calls, [])
        self.assertFalse(plugin.store.path.exists())

    def test_site_checkin_rejects_wrong_amount_boundaries_fields_and_conflicts(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        base = {"ok": True, "checked_in": True, "date": "2026-11-08", "amount": 5}
        responses = [(200, dict(base, amount=value)) for value in (-1, True, "5", 9007199254740992)]
        responses.extend((
            (200, {k: v for k, v in base.items() if k != "date"}),
            (200, dict(base, extra="ignored")),
            (409, {"ok": False, "checked_in": False, "date": "2026-11-08", "amount": 0, "reason": "event_conflict"}),
        ))
        client = site_api.SiteApiClient(
            {"site_base_url": "https://example.test", "service_key_id": "checkin", "service_secret": "c" * 32}
        )
        for status, body in responses:
            with self.subTest(status=status, body=body):
                client.transport = lambda *_args, status=status, body=body: (status, json.dumps(body).encode())
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.checkin("7001", "boundaries"))

    def test_site_client_posts_exact_signed_identity_request_with_stubbed_io(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        captured = {}

        def transport(url, body, headers):
            captured.update({"url": url, "body": body, "headers": headers})
            return 200, b'{"ok":true,"bound":true,"user":{"user_id":1,"tg_uid":"7001","status":"active"}}'

        client = site_api.SiteApiClient(
            {
                "site_base_url": "https://example.test/",
                "service_key_id": "astrbot-test",
                "service_secret": "x" * 32,
            },
            transport=transport,
        )
        with patch("astrbot_plugin_xingxy.site_api.service_signature.sign", return_value="f" * 64) as signer:
            identity = asyncio.run(client.resolve_identity("7001"))

        self.assertEqual(identity, {"tg_uid": "7001", "user_id": 1, "status": "active"})
        self.assertEqual(
            captured["url"],
            "https://example.test/wp-json/xingxy-ecosystem/v1/identity/resolve",
        )
        self.assertEqual(captured["body"], b'{"tg_uid":"7001"}')
        headers = captured["headers"]
        self.assertEqual(headers["X-Xingxy-Key-Id"], "astrbot-test")
        self.assertEqual(headers["X-Xingxy-Signature"], "f" * 64)
        self.assertEqual(len(headers["X-Xingxy-Nonce"]), 32)
        self.assertEqual(headers["Content-Type"], "application/json")
        signer.assert_called_once()
        args = signer.call_args.args
        self.assertEqual(args[0], b"x" * 32)
        self.assertEqual(args[1], "astrbot-test")
        self.assertEqual(args[4:7], ("POST", "/xingxy-ecosystem/v1/identity/resolve", b'{"tg_uid":"7001"}'))

    def test_points_client_validates_site_balance_and_uses_wordpress_source(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        body = {
            "ok": True,
            "bound": True,
            "user": {"user_id": 73, "tg_uid": "7001", "status": "active"},
            "balance": 41,
            "source": "wordpress",
            "ledger_enabled": False,
            "checkin_enabled": False,
        }

        def transport(_url, _body, _headers):
            return 200, json.dumps(body).encode()

        client = site_api.SiteApiClient(
            {"site_base_url": "https://example.test", "service_key_id": "points",
             "service_secret": "p" * 32}, transport=transport
        )
        result = asyncio.run(client.points_balance("7001"))
        self.assertEqual(result["user_id"], 73)
        self.assertEqual(result["balance"], 41)
        self.assertFalse(result["ledger_enabled"])
        self.assertFalse(result["checkin_enabled"])

        unbound = dict(body, bound=False, user=None, balance=None)
        client.transport = lambda *_args: (200, json.dumps(unbound).encode())
        result = asyncio.run(client.points_balance("7001"))
        self.assertIsNone(result["user_id"])
        self.assertIsNone(result["balance"])

    def test_points_client_rejects_malformed_identity_balance_and_rollout_flags(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        base = {
            "ok": True,
            "bound": True,
            "user": {"user_id": 73, "tg_uid": "7001", "status": "active"},
            "balance": 41,
            "source": "wordpress",
            "ledger_enabled": False,
            "checkin_enabled": False,
        }
        client = site_api.SiteApiClient(
            {"site_base_url": "https://example.test", "service_key_id": "points",
             "service_secret": "p" * 32},
            transport=lambda *_args: (200, b"{}"),
        )
        cases = []
        for invalid_balance in (True, "41", -1, 9007199254740992):
            cases.append(dict(base, balance=invalid_balance))
        cases.extend((
            dict(base, source="local"),
            dict(base, ledger_enabled="false"),
            dict(base, user=dict(base["user"], tg_uid="7002")),
            dict(base, bound=False, user=None, balance=0),
            {key: value for key, value in base.items() if key != "checkin_enabled"},
        ))
        for payload in cases:
            with self.subTest(payload=payload):
                client.transport = lambda *_args, payload=payload: (
                    200, json.dumps(payload).encode()
                )
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.points_balance("7001"))

    def test_points_balance_command_uses_separate_credentials_and_never_falls_back(self):
        config = {
            "points_read_enabled": True,
            "site_base_url": "https://example.test",
            "service_key_id": "identity-key",
            "service_secret": "i" * 32,
            "binding_service_key_id": "binding-key",
            "binding_service_secret": "b" * 32,
            "points_service_key_id": "points-key",
            "points_service_secret": "p" * 32,
        }
        plugin = self.module.XingxyPlugin(
            context=FakeContext(), config=config
        )
        calls = []

        def transport(url, body, headers):
            calls.append((url, body, headers))
            return 200, (b'{"ok":true,"bound":true,"user":'
                         b'{"user_id":73,"tg_uid":"7001","status":"active"},'
                         b'"balance":41,"source":"wordpress",'
                         b'"ledger_enabled":false,"checkin_enabled":false}')

        plugin.site_transport = transport
        output = _collect(plugin.cmd_balance(FakeEvent("7001")))[0]
        self.assertIn("站点积分余额：41", output)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], b'{"tg_uid":"7001"}')
        self.assertEqual(calls[0][2]["X-Xingxy-Key-Id"], "points-key")
        signature_module = sys.modules["astrbot_plugin_xingxy.service_signature"]
        headers = calls[0][2]
        self.assertEqual(
            headers["X-Xingxy-Signature"],
            signature_module.sign(
                b"p" * 32, "points-key", headers["X-Xingxy-Timestamp"],
                headers["X-Xingxy-Nonce"], "POST",
                "/xingxy-ecosystem/v1/points/balance", calls[0][1],
            ),
        )
        self.assertFalse(plugin.store.path.exists())

        plugin.config = dict(config, points_service_key_id="", points_service_secret="")
        calls.clear()
        output = _collect(plugin.cmd_balance(FakeEvent("7001")))[0]
        self.assertIn("凭据未就绪", output)
        self.assertEqual(calls, [])
        self.assertFalse(plugin.store.path.exists())

    def test_points_balance_command_requires_private_telegram_and_disables_trace(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(trace_enable=True),
            config={"points_read_enabled": True,
                    "site_base_url": "https://example.test",
                    "points_service_key_id": "points-key",
                    "points_service_secret": "p" * 32},
        )
        calls = []
        plugin.site_transport = lambda *args: calls.append(args)
        for event in (
            FakeEvent(group_id="-100123"),
            FakeEvent(platform="discord"),
        ):
            output = _collect(plugin.cmd_balance(event))[0]
            self.assertIn("仅接受 Telegram 私聊", output)
        output = _collect(plugin.cmd_balance(FakeEvent()))[0]
        self.assertIn("关闭 AstrBot 全局 trace_enable", output)
        self.assertEqual(calls, [])

    def test_points_balance_transport_failure_does_not_use_local_simulation(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(),
            config={
                "points_read_enabled": True,
                "site_base_url": "https://example.test",
                "points_service_key_id": "points-key",
                "points_service_secret": "p" * 32,
            },
        )
        plugin.store.checkin("7001", "synthetic-event", 10, True, 10001)
        before = plugin.store.path.read_bytes()
        plugin.site_transport = lambda *_args: (503, b"private error response")
        output = _collect(plugin.cmd_balance(FakeEvent("7001")))[0]
        self.assertIn("查询暂不可用", output)
        self.assertNotIn("积分余额：10", output)
        self.assertEqual(plugin.store.path.read_bytes(), before)

    def test_site_response_rejects_bad_shape_and_sender_mismatch(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        with self.assertRaises(site_api.SiteApiError):
            site_api.validate_identity_response(
                {"ok": True, "bound": "true", "user": None}, "7001"
            )
        with self.assertRaises(site_api.SiteApiError):
            site_api.validate_identity_response(
                {"ok": True, "bound": True, "user": {"user_id": 1, "tg_uid": "7002", "status": "active"}},
                "7001",
            )

    def test_site_identity_is_read_only_and_gated_to_private_telegram(self):
        config = {
            "backend_mode": "local",
            "site_base_url": "https://example.test",
            "service_key_id": "astrbot-test",
            "service_secret": "x" * 32,
        }
        plugin = self.module.XingxyPlugin(context=None, config=config)
        calls = []

        def transport(url, body, headers):
            calls.append((url, body, headers))
            return 200, b'{"ok":true,"bound":true,"user":{"user_id":1,"tg_uid":"7001","status":"active"}}'

        plugin.site_transport = transport
        group = _collect(plugin.cmd_site_identity(FakeEvent(group_id="-100123")))[0]
        other_platform = _collect(plugin.cmd_site_identity(FakeEvent(platform="discord")))[0]
        self.assertIn("仅接受 Telegram 私聊", group)
        self.assertIn("仅接受 Telegram 私聊", other_platform)
        self.assertEqual(calls, [])

        output = _collect(plugin.cmd_site_identity(FakeEvent()))[0]
        self.assertIn("数据来源：Xingxy 网站（只读）", output)
        self.assertIn("网站 user_id：1", output)
        self.assertNotIn("积分余额", output)
        self.assertEqual(len(calls), 1)
        self.assertFalse(plugin.store.path.exists())

    def test_site_mode_identity_uses_site_and_checkin_refuses_without_local_write(self):
        config = {
            "backend_mode": "site",
            "site_base_url": "https://example.test",
            "service_key_id": "astrbot-test",
            "service_secret": "x" * 32,
        }
        plugin = self.module.XingxyPlugin(context=None, config=config)
        calls = []

        def transport(url, body, headers):
            calls.append(url)
            return 200, b'{"ok":true,"bound":false,"user":null}'

        plugin.site_transport = transport
        identity = _collect(plugin.cmd_identity(FakeEvent()))[0]
        self.assertIn("数据来源：Xingxy 网站（只读）", identity)
        self.assertIn("绑定状态：unbound", identity)
        self.assertEqual(len(calls), 1)

        checkin = _collect(plugin.cmd_checkin(FakeEvent(), "must-not-write"))[0]
        self.assertIn("网站真实签到当前未启用", checkin)
        self.assertEqual(len(calls), 1)
        self.assertFalse(plugin.store.path.exists())

    def test_site_identity_failure_does_not_fall_back_to_local_data(self):
        plugin = self.module.XingxyPlugin(
            context=None,
            config={
                "backend_mode": "site",
                "site_base_url": "https://example.test",
                "service_key_id": "astrbot-test",
                "service_secret": "x" * 32,
            },
        )

        def failed_transport(_url, _body, _headers):
            raise OSError("private URL and response details")

        plugin.site_transport = failed_transport
        output = _collect(plugin.cmd_identity(FakeEvent()))[0]
        self.assertIn("网站身份查询暂不可用", output)
        self.assertNotIn("private URL", output)
        self.assertNotIn("测试 user_id", output)
        self.assertFalse(plugin.store.path.exists())

    def test_link_start_posts_exact_payload_and_validates_oauth_url(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        captured = {}
        site_origin = "https://example.test"
        params = {
            "client_id": "dedicated-link-client",
            "response_type": "code",
            "scope": "basic",
            "redirect_uri": site_origin + site_api.OAUTH_CALLBACK_PATH,
            "state": "s" * 43,
            "code_challenge": "c" * 43,
            "code_challenge_method": "S256",
        }
        authorization_url = (
            site_origin
            + site_api.OAUTH_AUTHORIZE_PATH
            + "?"
            + urlencode(params)
        )

        def transport(url, body, headers):
            captured.update({"url": url, "body": body, "headers": headers})
            response = {
                "ok": True,
                "status": "authorization_required",
                "authorization_url": authorization_url,
                "expires_in": 600,
            }
            return 200, json.dumps(response).encode("utf-8")

        client = site_api.SiteApiClient(
            {
                "site_base_url": site_origin,
                "service_key_id": "astrbot-test",
                "service_secret": "x" * 32,
            },
            transport=transport,
        )
        with patch(
            "astrbot_plugin_xingxy.site_api.service_signature.sign",
            return_value="f" * 64,
        ) as signer:
            result = asyncio.run(client.start_link("7001"))

        self.assertEqual(result["status"], "authorization_required")
        self.assertEqual(result["authorization_url"], authorization_url)
        self.assertEqual(
            captured["url"],
            site_origin + "/wp-json" + site_api.LINK_START_ROUTE,
        )
        self.assertEqual(captured["body"], b'{"tg_uid":"7001"}')
        signer.assert_called_once()
        self.assertEqual(signer.call_args.args[5], site_api.LINK_START_ROUTE)

    def test_link_start_already_bound_requires_matching_active_user(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        result = site_api.validate_link_start_response(
            {
                "ok": True,
                "status": "already_bound",
                "bound": True,
                "user": {"user_id": 1, "tg_uid": "7001", "status": "active"},
            },
            "7001",
            "https://example.test",
        )
        self.assertEqual(result["identity"]["user_id"], 1)
        for bad_payload in (
            {
                "ok": True,
                "status": "already_bound",
                "bound": False,
                "user": {"user_id": 1, "tg_uid": "7001", "status": "active"},
            },
            {
                "ok": True,
                "status": "already_bound",
                "bound": True,
                "user": {"user_id": 1, "tg_uid": "7002", "status": "active"},
            },
        ):
            with self.assertRaises(site_api.SiteApiError):
                site_api.validate_link_start_response(
                    bad_payload, "7001", "https://example.test"
                )

    def test_link_start_rejects_unsafe_or_unexpected_authorization_urls(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        origin = "https://example.test"
        params = {
            "client_id": "dedicated-link-client",
            "response_type": "code",
            "scope": "basic",
            "redirect_uri": origin + site_api.OAUTH_CALLBACK_PATH,
            "state": "s" * 43,
            "code_challenge": "c" * 43,
            "code_challenge_method": "S256",
        }
        valid_url = origin + site_api.OAUTH_AUTHORIZE_PATH + "?" + urlencode(params)
        unsafe_urls = (
            valid_url.replace(origin, "https://evil.test"),
            valid_url.replace(origin, "https://user@example.test"),
            valid_url.replace(site_api.OAUTH_AUTHORIZE_PATH, "/unsafe"),
            valid_url + "#fragment",
            valid_url + "#",
            valid_url + "&prompt=none",
            valid_url + "&scope=other",
            valid_url.replace("code_challenge_method=S256", "code_challenge_method=plain"),
            valid_url.replace("scope=basic", "scope=other"),
        )
        for authorization_url in unsafe_urls:
            with self.subTest(authorization_url=authorization_url[:48]):
                with self.assertRaises(site_api.SiteApiError):
                    site_api.validate_link_start_response(
                        {
                            "ok": True,
                            "status": "authorization_required",
                            "authorization_url": authorization_url,
                            "expires_in": 600,
                        },
                        "7001",
                        origin,
                    )

    def test_confirm_link_posts_exact_body_and_validates_identity(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        captured = {}
        confirmation_code = "A1b2C3d4E5f6G7h8"

        def transport(url, body, headers):
            captured.update({"url": url, "body": body, "headers": headers})
            return 200, b'{"ok":true,"bound":true,"user":{"user_id":42,"tg_uid":"7001","status":"active"}}'

        client = site_api.SiteApiClient(
            {
                "site_base_url": "https://example.test",
                "service_key_id": "astrbot-test",
                "service_secret": "x" * 32,
            },
            transport=transport,
        )
        with patch(
            "astrbot_plugin_xingxy.site_api.service_signature.sign",
            return_value="f" * 64,
        ) as signer:
            identity = asyncio.run(
                client.confirm_link("7001", confirmation_code)
            )

        self.assertEqual(identity["user_id"], 42)
        self.assertEqual(
            captured["url"],
            "https://example.test/wp-json" + site_api.IDENTITY_BIND_ROUTE,
        )
        self.assertEqual(
            captured["body"],
            b'{"tg_uid":"7001","confirmation_code":"A1b2C3d4E5f6G7h8"}',
        )
        self.assertEqual(signer.call_args.args[5], site_api.IDENTITY_BIND_ROUTE)

    def test_confirm_link_rejects_bad_code_before_http(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        calls = []
        client = site_api.SiteApiClient(
            {
                "site_base_url": "https://example.test",
                "service_key_id": "astrbot-test",
                "service_secret": "x" * 32,
            },
            transport=lambda *_args: calls.append(_args),
        )
        with self.assertRaises(site_api.SiteApiError):
            asyncio.run(client.confirm_link("7001", "bad-code"))
        self.assertEqual(calls, [])

    def test_bind_commands_are_feature_gated_private_only_and_site_only(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        config = {
            "backend_mode": "local",
            "binding_enabled": True,
            "site_base_url": "https://example.test",
            "service_key_id": "read-key",
            "service_secret": "r" * 32,
            "binding_service_key_id": "binding-key",
            "binding_service_secret": "b" * 32,
        }
        context = FakeContext(trace_enable=False)
        plugin = self.module.XingxyPlugin(context=context, config=config)
        calls = []
        site_origin = "https://example.test"
        authorization_url = site_origin + site_api.OAUTH_AUTHORIZE_PATH + "?" + urlencode(
            {
                "client_id": "dedicated-link-client",
                "response_type": "code",
                "scope": "basic",
                "redirect_uri": site_origin + site_api.OAUTH_CALLBACK_PATH,
                "state": "s" * 43,
                "code_challenge": "c" * 43,
                "code_challenge_method": "S256",
            }
        )

        def transport(url, body, headers):
            calls.append((url, body, headers))
            if url.endswith("/identity/resolve"):
                return 200, b'{"ok":true,"bound":false,"user":null}'
            if url.endswith("/identity/link/start"):
                return 200, json.dumps(
                    {
                        "ok": True,
                        "status": "authorization_required",
                        "authorization_url": authorization_url,
                        "expires_in": 600,
                    }
                ).encode("utf-8")
            return 200, b'{"ok":true,"bound":true,"user":{"user_id":42,"tg_uid":"7001","status":"active"}}'

        plugin.site_transport = transport
        disabled = self.module.XingxyPlugin(context=None, config={})
        disabled.site_transport = transport
        output = _collect(disabled.cmd_bind(FakeEvent()))[0]
        self.assertIn("当前未启用", output)
        self.assertEqual(calls, [])

        for event in (
            FakeEvent(group_id="-100123"),
            FakeEvent(platform="discord"),
        ):
            output = _collect(plugin.cmd_bind(event))[0]
            self.assertIn("仅接受 Telegram 私聊", output)
        self.assertEqual(calls, [])

        start = _collect(plugin.cmd_bind(FakeEvent()))[0]
        self.assertIn(authorization_url, start)
        self.assertIn("已有绑定也可以重新授权", start)
        self.assertIn("本私聊", start)
        confirmed = _collect(
            plugin.cmd_bind_confirm(FakeEvent(), "A1b2C3d4E5f6G7h8")
        )[0]
        self.assertIn("网站账号绑定成功", confirmed)
        self.assertIn("网站 user_id：42", confirmed)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1], b'{"tg_uid":"7001"}')
        self.assertEqual(calls[0][2]["X-Xingxy-Key-Id"], "read-key")
        self.assertEqual(
            calls[1][1],
            b'{"tg_uid":"7001"}',
        )
        self.assertEqual(calls[1][2]["X-Xingxy-Key-Id"], "binding-key")
        self.assertEqual(
            calls[2][1],
            b'{"tg_uid":"7001","confirmation_code":"A1b2C3d4E5f6G7h8"}',
        )
        self.assertEqual(calls[2][2]["X-Xingxy-Key-Id"], "binding-key")
        self.assertFalse(plugin.store.path.exists())
        self.assertIn(None, context.config_reads)

        deep_link_event = FakeEvent(message="/start xybind_A1b2C3d4E5f6G7h8")
        deep_link = _collect(plugin.cmd_bind_deep_link(deep_link_event))[0]
        self.assertIn("网站账号绑定成功", deep_link)
        self.assertTrue(deep_link_event.stopped)
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            calls[3][1],
            b'{"tg_uid":"7001","confirmation_code":"A1b2C3d4E5f6G7h8"}',
        )

        bare_start_event = FakeEvent(message="/start")
        bare_start = _collect(plugin.cmd_bind_deep_link(bare_start_event))[0]
        self.assertIn("网站账号绑定成功", bare_start)
        self.assertTrue(bare_start_event.stopped)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[4][1], b'{"tg_uid":"7001"}')

    def test_bind_command_sanitizes_failure_and_never_echoes_code(self):
        config = {
            "binding_enabled": True,
            "site_base_url": "https://example.test",
            "binding_service_key_id": "binding-key",
            "binding_service_secret": "b" * 32,
        }
        plugin = self.module.XingxyPlugin(
            context=FakeContext(trace_enable=False),
            config=config,
        )

        def failed_transport(_url, _body, _headers):
            raise OSError("raw private response and credentials")

        plugin.site_transport = failed_transport
        code = "A1b2C3d4E5f6G7h8"
        output = _collect(plugin.cmd_bind_confirm(FakeEvent(), code))[0]
        self.assertIn("绑定未完成", output)
        self.assertNotIn(code, output)
        self.assertNotIn("raw private response", output)
        self.assertFalse(plugin.store.path.exists())

    def test_bind_does_not_reauthorize_existing_identity_unless_verify_command_is_used(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        site_origin = "https://example.test"
        authorization_url = site_origin + site_api.OAUTH_AUTHORIZE_PATH + "?" + urlencode(
            {
                "client_id": "dedicated-link-client",
                "response_type": "code",
                "scope": "basic",
                "redirect_uri": site_origin + site_api.OAUTH_CALLBACK_PATH,
                "state": "s" * 43,
                "code_challenge": "c" * 43,
                "code_challenge_method": "S256",
            }
        )
        config = {
            "binding_enabled": True,
            "site_base_url": site_origin,
            "service_key_id": "read-key",
            "service_secret": "r" * 32,
            "binding_service_key_id": "binding-key",
            "binding_service_secret": "b" * 32,
        }
        plugin = self.module.XingxyPlugin(FakeContext(), config)
        calls = []

        def transport(url, body, headers):
            calls.append((url, headers["X-Xingxy-Key-Id"]))
            if url.endswith("/identity/resolve"):
                return 200, b'{"ok":true,"bound":true,"user":{"user_id":42,"tg_uid":"7001","status":"active"}}'
            return 200, json.dumps({
                "ok": True,
                "status": "authorization_required",
                "authorization_url": authorization_url,
                "expires_in": 600,
            }).encode()

        plugin.site_transport = transport
        normal = _collect(plugin.cmd_bind(FakeEvent()))[0]
        self.assertIn("已绑定网站 user_id：42", normal)
        self.assertIn("/xy_bind_verify", normal)
        self.assertEqual(calls, [("https://example.test/wp-json/xingxy-ecosystem/v1/identity/resolve", "read-key")])

        verify = _collect(plugin.cmd_bind_verify(FakeEvent()))[0]
        self.assertIn(authorization_url, verify)
        self.assertEqual(calls[-1][1], "binding-key")

    def test_read_and_binding_commands_use_separate_credentials_without_fallback(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        site_origin = "https://example.test"
        authorization_url = site_origin + site_api.OAUTH_AUTHORIZE_PATH + "?" + urlencode(
            {
                "client_id": "dedicated-link-client",
                "response_type": "code",
                "scope": "basic",
                "redirect_uri": site_origin + site_api.OAUTH_CALLBACK_PATH,
                "state": "s" * 43,
                "code_challenge": "c" * 43,
                "code_challenge_method": "S256",
            }
        )
        config = {
            "backend_mode": "local",
            "binding_enabled": True,
            "site_base_url": site_origin,
            "service_key_id": "read-key",
            "service_secret": "r" * 32,
            "binding_service_key_id": "binding-key",
            "binding_service_secret": "b" * 32,
        }
        plugin = self.module.XingxyPlugin(
            context=FakeContext(trace_enable=False),
            config=config,
        )
        calls = []

        def transport(url, body, headers):
            calls.append((url, body, headers))
            if url.endswith("/identity/resolve"):
                response = {
                    "ok": True,
                    "bound": False,
                    "user": None,
                }
            elif url.endswith("/identity/link/start"):
                response = {
                    "ok": True,
                    "status": "authorization_required",
                    "authorization_url": authorization_url,
                    "expires_in": 600,
                }
            else:
                response = {
                    "ok": True,
                    "bound": True,
                    "user": {"user_id": 12, "tg_uid": "7001", "status": "active"},
                }
            return 200, json.dumps(response).encode("utf-8")

        plugin.site_transport = transport
        _collect(plugin.cmd_site_identity(FakeEvent()))
        _collect(plugin.cmd_bind(FakeEvent()))
        _collect(plugin.cmd_bind_confirm(FakeEvent(), "A1b2C3d4E5f6G7h8"))
        self.assertEqual(
            [call[2]["X-Xingxy-Key-Id"] for call in calls],
            ["read-key", "read-key", "binding-key", "binding-key"],
        )
        self.assertEqual(calls[0][1], b'{"tg_uid":"7001"}')
        self.assertEqual(calls[1][1], b'{"tg_uid":"7001"}')
        self.assertEqual(
            calls[2][1],
            b'{"tg_uid":"7001"}',
        )
        self.assertEqual(
            calls[3][1],
            b'{"tg_uid":"7001","confirmation_code":"A1b2C3d4E5f6G7h8"}',
        )
        self.assertFalse(plugin.store.path.exists())

        read_only_config = {
            key: value
            for key, value in config.items()
            if key not in ("binding_service_key_id", "binding_service_secret")
        }
        read_only_plugin = self.module.XingxyPlugin(
            context=FakeContext(trace_enable=False),
            config=read_only_config,
        )
        fallback_calls = []

        def read_transport(url, body, headers):
            fallback_calls.append((url, headers))
            return 200, b'{"ok":true,"bound":false,"user":null}'

        read_only_plugin.site_transport = read_transport
        bind_output = _collect(read_only_plugin.cmd_bind(FakeEvent()))[0]
        self.assertIn("尚未配置完整", bind_output)
        self.assertEqual(fallback_calls, [])
        _collect(read_only_plugin.cmd_site_identity(FakeEvent()))
        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(fallback_calls[0][1]["X-Xingxy-Key-Id"], "read-key")

    def test_binding_commands_fail_closed_when_global_trace_logging_is_enabled(self):
        calls = []
        plugin = self.module.XingxyPlugin(
            context=FakeContext(trace_enable=True),
            config={
                "binding_enabled": True,
                "site_base_url": "https://example.test",
                "binding_service_key_id": "binding-key",
                "binding_service_secret": "b" * 32,
            },
        )
        plugin.site_transport = lambda *args: calls.append(args)
        start = _collect(plugin.cmd_bind(FakeEvent()))[0]
        confirm = _collect(
            plugin.cmd_bind_confirm(FakeEvent(), "A1b2C3d4E5f6G7h8")
        )[0]
        self.assertIn("AstrBot trace logging", start)
        self.assertIn("AstrBot trace logging", confirm)
        self.assertEqual(calls, [])
        self.assertFalse(plugin.store.path.exists())

    def test_binding_log_filter_redacts_confirm_command_in_formatted_records(self):
        redactor = sys.modules["astrbot_plugin_xingxy.log_redactor"]
        log_filter = self.plugin._core_log_filter
        logger = logging.getLogger("astrbot")
        self.assertIn(log_filter, logger.filters)

        code = "A1b2C3d4E5f6G7h8"
        inbound_outlines = (
            "/xy_bind_confirm " + code,
            "xy_bind_confirm@xingxybot\t" + code,
            "xy_bind_confirm  @xingxybot\n" + code,
        )
        for outline in inbound_outlines:
            with self.subTest(outline=outline.splitlines()[0]):
                record = logging.LogRecord(
                    "astrbot",
                    logging.INFO,
                    __file__,
                    1,
                    "Inbound outline: %s",
                    (outline,),
                    None,
                )
                self.assertTrue(log_filter.filter(record))
                formatted = record.getMessage()
                self.assertNotIn(code, formatted)
                self.assertIn("[REDACTED]", formatted)
                self.assertEqual(record.args, ())

        harmless = "xy_bind_confirm is documented in https://example.test/help"
        self.assertEqual(redactor.redact_sensitive_text(harmless), harmless)
        balance_log = "site reply: 站点积分余额：4051"
        redacted_balance = redactor.redact_sensitive_text(balance_log)
        self.assertNotIn("4051", redacted_balance)
        self.assertIn("站点积分余额：[REDACTED]", redacted_balance)

    def test_terminate_removes_only_plugin_log_filter(self):
        logger = logging.getLogger("astrbot")
        own_filter = self.plugin._core_log_filter
        unrelated_filter = logging.Filter("unrelated")
        logger.addFilter(unrelated_filter)
        try:
            self.assertIn(own_filter, logger.filters)
            asyncio.run(self.plugin.terminate())
            self.assertNotIn(own_filter, logger.filters)
            self.assertIn(unrelated_filter, logger.filters)
        finally:
            logger.removeFilter(unrelated_filter)

    def test_core_logger_queue_receives_redacted_authorization_url(self):
        logger = logging.getLogger("astrbot")
        original_handlers = list(logger.handlers)
        original_level = logger.level
        original_propagate = logger.propagate
        output_queue = queue.Queue()
        queue_handler = QueueHandler(output_queue)
        authorization_url = (
            "https://example.test/wp-json/zibll-oauth/v1/authorize?"
            "client_id=dummy&state=state-value&code_challenge=challenge-value"
        )
        logger.handlers[:] = [queue_handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info("Prepare to send: %s", authorization_url)
            queued_record = output_queue.get(timeout=1)
            logged_text = queued_record.getMessage()
            self.assertNotIn("state-value", logged_text)
            self.assertNotIn("challenge-value", logged_text)
            self.assertIn("[REDACTED OAuth authorization URL]", logged_text)
            # Redaction is limited to the log record; an actual Telegram result
            # remains intact, as covered by the command response assertion.
            self.assertIn("state-value", authorization_url)
        finally:
            logger.handlers[:] = original_handlers
            logger.setLevel(original_level)
            logger.propagate = original_propagate

    def test_site_configuration_rejects_non_https_path_and_short_secret(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        base_config = {
            "site_base_url": "https://example.test",
            "service_key_id": "astrbot-test",
            "service_secret": "x" * 32,
        }
        self.assertEqual(site_api.configuration_issues(base_config), ())
        for base_url in (
            "http://example.test",
            "https://user:pass@example.test",
            "https://example.test/path",
            "https://example.test?token=secret",
            "https://example.test/#fragment",
        ):
            config = dict(base_config, site_base_url=base_url)
            self.assertIn("site_base_url 无效", " ".join(site_api.configuration_issues(config)))
        config = dict(base_config, service_secret="short")
        self.assertIn("service_secret 长度无效", " ".join(site_api.configuration_issues(config)))

    def test_default_http_transport_uses_total_timeout_no_redirect_and_bounded_read(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        captured = {}
        response_body = b'{"ok":true,"bound":false,"user":null}'

        class FakeTimeout:
            def __init__(self, total):
                captured["timeout_total"] = total

        class FakeContent:
            def __init__(self, content):
                self.content = content
                self.offset = 0

            async def read(self, size):
                captured.setdefault("read_sizes", []).append(size)
                # Simulate a slow/chunked response that often yields less than
                # the requested amount before EOF.
                read_size = min(size, 5, len(self.content) - self.offset)
                chunk = self.content[self.offset : self.offset + read_size]
                self.offset += len(chunk)
                return chunk

        class FakeResponse:
            status = 302

            def __init__(self, content):
                self.content = FakeContent(content)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeSession:
            def __init__(self, timeout):
                captured["session_timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **kwargs):
                captured["url"] = url
                captured.update(kwargs)
                return FakeResponse(response_body)

        fake_aiohttp = types.SimpleNamespace(
            ClientTimeout=FakeTimeout,
            ClientSession=FakeSession,
        )
        with patch.dict("sys.modules", {"aiohttp": fake_aiohttp}):
            status, body = asyncio.run(
                site_api._http_post(
                    "https://example.test/wp-json/identity",
                    b"{}",
                    {"X-Xingxy-Signature": "sensitive"},
                )
            )

        self.assertEqual(status, 302)
        self.assertEqual(body, response_body)
        self.assertEqual(captured["timeout_total"], 10)
        self.assertIsInstance(captured["session_timeout"], FakeTimeout)
        self.assertIs(captured["allow_redirects"], False)
        self.assertGreater(len(captured["read_sizes"]), 1)

    def test_oversized_body_with_valid_json_prefix_is_rejected(self):
        site_api = sys.modules["astrbot_plugin_xingxy.site_api"]
        valid_prefix = b'{"ok":true,"bound":false,"user":null}'
        oversized_body = valid_prefix + b" " * (site_api.MAX_RESPONSE_BYTES + 1)

        class FakeTimeout:
            def __init__(self, total):
                self.total = total

        class FakeContent:
            def __init__(self):
                self.offset = 0

            async def read(self, size):
                chunk = oversized_body[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        class FakeResponse:
            status = 200

            def __init__(self):
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeSession:
            def __init__(self, timeout):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, **_kwargs):
                return FakeResponse()

        fake_aiohttp = types.SimpleNamespace(
            ClientTimeout=FakeTimeout,
            ClientSession=FakeSession,
        )
        with patch.dict("sys.modules", {"aiohttp": fake_aiohttp}):
            client = site_api.SiteApiClient(
                {
                    "site_base_url": "https://example.test",
                    "service_key_id": "astrbot-test",
                    "service_secret": "x" * 32,
                }
            )
            with patch(
                "astrbot_plugin_xingxy.site_api.service_signature.sign",
                return_value="f" * 64,
            ):
                with self.assertRaises(site_api.SiteApiError):
                    asyncio.run(client.resolve_identity("7001"))

    def test_status_reports_mode_and_missing_config_without_secret_or_url(self):
        plugin = self.module.XingxyPlugin(
            context=None,
            config={
                "backend_mode": "site",
                "site_base_url": "https://private.example.test",
                "service_key_id": "private-key-id",
                "service_secret": "s" * 32,
                "binding_service_key_id": "private-binding-key-id",
                "binding_service_secret": "b" * 32,
            },
        )
        output = _collect(plugin.cmd_status(FakeEvent()))[0]
        self.assertIn("运行模式：site", output)
        self.assertIn("身份读取 API 凭据完整", output)
        self.assertIn("账号绑定 API 凭据完整", output)
        for private_value in (
            "private.example.test",
            "private-key-id",
            "private-binding-key-id",
            "s" * 32,
            "b" * 32,
        ):
            self.assertNotIn(private_value, output)

        missing = self.module.XingxyPlugin(context=None, config={})
        output = _collect(missing.cmd_status(FakeEvent()))[0]
        for key in (
            "site_base_url",
            "service_key_id",
            "service_secret",
            "binding_service_key_id",
            "binding_service_secret",
        ):
            self.assertIn(key, output)

    def test_binding_service_secret_schema_is_empty_and_password_formatted(self):
        schema_path = PLUGIN_DIR / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["binding_service_key_id"]["type"], "string")
        self.assertEqual(schema["binding_service_key_id"]["default"], "")
        self.assertEqual(schema["binding_service_secret"]["type"], "string")
        self.assertEqual(schema["binding_service_secret"]["default"], "")
        self.assertEqual(schema["binding_service_secret"]["format"], "password")

    def test_checkin_status_help_and_schema_report_feature_without_secrets(self):
        plugin = self.module.XingxyPlugin(
            context=FakeContext(),
            config={
                "backend_mode": "site",
                "site_checkin_enabled": True,
                "site_base_url": "https://private.example.test",
                "checkin_service_key_id": "private-checkin-key",
                "checkin_service_secret": "c" * 32,
            },
        )
        status = _collect(plugin.cmd_status(FakeEvent()))[0]
        self.assertIn("网站签到：已启用；专用凭据完整", status)
        help_text = _collect(plugin.cmd_help(FakeEvent()))[0]
        self.assertIn("网站签到功能已启用", help_text)
        for private_value in (
            "private.example.test", "private-checkin-key", "c" * 32,
        ):
            self.assertNotIn(private_value, status)
            self.assertNotIn(private_value, help_text)

        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIs(schema["site_checkin_enabled"]["default"], False)
        self.assertEqual(schema["checkin_service_key_id"]["default"], "")
        self.assertEqual(schema["checkin_service_secret"]["default"], "")
        self.assertEqual(schema["checkin_service_secret"]["format"], "password")


if __name__ == "__main__":
    unittest.main()
