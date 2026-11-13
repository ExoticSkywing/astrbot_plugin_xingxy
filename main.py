"""Xingxy site identity, check-in, points and isolated local simulation."""

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from .log_redactor import install_core_log_filter, remove_core_log_filter
from .site_api import (
    CONFIRMATION_CODE_RE,
    SiteApiClient,
    SiteApiError,
    SiteBindingConflictError,
    SiteBindingConfirmationError,
    configuration_issues,
)

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_NAME = "astrbot_plugin_xingxy"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
EVENT_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
# WakingCheckStage removes the configured '/' prefix before handler filters run.
START_BIND_PATTERN = r"\A/?start(?:@[A-Za-z0-9_]+)?\s+xybind_([A-Za-z0-9_-]{16})\Z"


def shanghai_date(now: Optional[datetime] = None) -> str:
    """Return today's date in Asia/Shanghai, including for a supplied aware time."""
    current = now if now is not None else datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    return current.astimezone(SHANGHAI_TZ).date().isoformat()


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "identities": {},
        "points": {},
        "checkin_events": {},
        "checkin_by_day": {},
    }


class JsonTestStore:
    """Small JSON store used only for this plugin's synthetic test data."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return _empty_state()
        with self.path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("测试数据文件格式无效")
        for key in ("identities", "points", "checkin_events", "checkin_by_day"):
            if not isinstance(state.get(key), dict):
                raise ValueError("测试数据文件格式无效")
        return state

    def save(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(str(temp_path), str(self.path))
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def reset(self) -> None:
        self.save(_empty_state())

    def identity(
        self,
        tg_uid: str,
        default_bound: bool,
        default_test_user_id: int,
    ) -> Dict[str, Any]:
        state = self.load()
        identities = state["identities"]
        identity = identities.get(tg_uid)
        changed = False
        if identity is None:
            identity = {
                "tg_uid": tg_uid,
                "user_id": default_test_user_id if default_bound else None,
                "status": "active" if default_bound else "unbound",
            }
            identities[tg_uid] = identity
            changed = True
        if identity.get("status") == "active" and identity.get("user_id") is not None:
            balance_key = str(identity["user_id"])
            if balance_key not in state["points"]:
                state["points"][balance_key] = 0
                changed = True
        if changed:
            self.save(state)
        return dict(identity)

    def data_summary(self) -> Dict[str, Any]:
        state = self.load()
        return {
            "identities": len(state["identities"]),
            "events": len(state["checkin_events"]),
            "points": dict(state["points"]),
        }

    def checkin(
        self,
        tg_uid: str,
        event_id: str,
        reward_points: int,
        default_bound: bool,
        default_test_user_id: int,
        today: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Apply an idempotent simulated check-in and return (outcome, event)."""
        state = self.load()
        identities = state["identities"]
        identity = identities.get(tg_uid)
        if identity is None:
            identity = {
                "tg_uid": tg_uid,
                "user_id": default_test_user_id if default_bound else None,
                "status": "active" if default_bound else "unbound",
            }
            identities[tg_uid] = identity

        previous = state["checkin_events"].get(event_id)
        if previous is not None:
            if previous.get("tg_uid") != tg_uid:
                return "event_id_conflict", dict(previous)
            return "replay", dict(previous)

        checkin_date = today or shanghai_date()
        user_id = identity.get("user_id") if identity.get("status") == "active" else None
        balance_key = str(user_id) if user_id is not None else None
        if user_id is None:
            result = {
                "event_id": event_id,
                "tg_uid": tg_uid,
                "user_id": None,
                "date": checkin_date,
                "status": "unbound",
                "reward_points": 0,
                "balance_after": None,
            }
            state["checkin_events"][event_id] = result
            self.save(state)
            return "unbound", dict(result)

        state["points"].setdefault(balance_key, 0)
        day_key = "{}:{}".format(user_id, checkin_date)
        first_event_id = state["checkin_by_day"].get(day_key)
        if first_event_id is not None:
            result = {
                "event_id": event_id,
                "tg_uid": tg_uid,
                "user_id": user_id,
                "date": checkin_date,
                "status": "already_checked_in",
                "reward_points": 0,
                "balance_after": state["points"][balance_key],
                "first_event_id": first_event_id,
            }
            state["checkin_events"][event_id] = result
            self.save(state)
            return "already_checked_in", dict(result)

        state["points"][balance_key] += reward_points
        result = {
            "event_id": event_id,
            "tg_uid": tg_uid,
            "user_id": user_id,
            "date": checkin_date,
            "status": "success",
            "reward_points": reward_points,
            "balance_after": state["points"][balance_key],
        }
        state["checkin_events"][event_id] = result
        state["checkin_by_day"][day_key] = event_id
        self.save(state)
        return "success", dict(result)


@register(
    PLUGIN_NAME,
    "Xingxy",
    "Xingxy 站点身份、积分、账号绑定、签到与本地模拟。",
    "v0.4.10",
    "https://github.com/ExoticSkywing/astrbot_plugin_xingxy",
)
class XingxyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        # Startup only prepares this plugin-owned test directory. The JSON file is
        # written lazily when a command first creates or changes simulated data.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = JsonTestStore(self.data_dir / "test_data.json")
        # Tests may replace this with an in-memory transport. Runtime requests use
        # SiteApiClient's asynchronous aiohttp transport.
        self.site_transport = None
        self._core_log_filter = install_core_log_filter()

    async def initialize(self):
        self._core_log_filter = install_core_log_filter()
        if self._binding_enabled() and self._trace_logging_enabled():
            logger.warning(
                "[xingxy] 网站账号绑定命令已暂停：AstrBot trace logging 当前已启用"
            )
        logger.info("[xingxy] 插件已初始化；启动时未连接外部服务")

    async def terminate(self):
        if self._core_log_filter is not None:
            remove_core_log_filter(self._core_log_filter)
            self._core_log_filter = None
        return None

    def _enabled(self) -> bool:
        return self.config.get("enabled", True) is not False

    def _backend_mode(self) -> str:
        value = self.config.get("backend_mode", "local")
        return value if value in ("local", "site") else "invalid"

    def _binding_enabled(self) -> bool:
        return self.config.get("binding_enabled", False) is True

    def _trace_logging_enabled(self) -> bool:
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return True
        try:
            global_config = getter()
        except Exception:
            return True
        if not hasattr(global_config, "get"):
            return True
        return bool(global_config.get("trace_enable", False))

    def _default_bound(self) -> bool:
        return self.config.get("default_bound", True) is not False

    def _test_user_id(self) -> int:
        value = self.config.get("default_test_user_id", 10001)
        if isinstance(value, bool):
            return 10001
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 10001
        return parsed if parsed > 0 else 10001

    def _reward_points(self) -> int:
        value = self.config.get("reward_points", 10)
        if isinstance(value, bool):
            return 10
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 10
        return parsed if parsed >= 0 else 10

    def _tg_uid(self, event: AstrMessageEvent) -> Optional[str]:
        getter = getattr(event, "get_sender_id", None)
        sender = getter() if callable(getter) else None
        if sender is None:
            return None
        tg_uid = str(sender).strip()
        return tg_uid or None

    def _local_identity(self, event: AstrMessageEvent) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        tg_uid = self._tg_uid(event)
        if not tg_uid:
            return None, "无法读取当前发送者的 TG UID。"
        identity = self.store.identity(
            tg_uid,
            self._default_bound(),
            self._test_user_id(),
        )
        return identity, None

    def _is_telegram_private(self, event: AstrMessageEvent) -> bool:
        """Allow site reads only for private events from AstrBot's Telegram adapter."""
        platform_getter = getattr(event, "get_platform_name", None)
        message_type_getter = getattr(event, "get_message_type", None)
        group_getter = getattr(event, "get_group_id", None)
        if not all(callable(value) for value in (platform_getter, message_type_getter, group_getter)):
            return False
        try:
            platform_name = platform_getter()
            message_type = message_type_getter()
            group_id = group_getter()
        except Exception:
            return False
        message_type_value = getattr(message_type, "value", message_type)
        return (
            platform_name == "telegram"
            and message_type_value == "FriendMessage"
            and not group_id
        )

    async def _site_identity_text(self, event: AstrMessageEvent) -> str:
        if not self._is_telegram_private(event):
            return "网站身份查询仅接受 Telegram 私聊消息。"
        tg_uid = self._tg_uid(event)
        if (
            not tg_uid
            or not tg_uid.isascii()
            or not tg_uid.isdigit()
            or int(tg_uid) <= 0
            or int(tg_uid) > 9223372036854775807
        ):
            return "无法读取有效的 Telegram 用户 ID。"
        issues = configuration_issues(self.config)
        if issues:
            return "网站 API 配置未就绪：\n" + "\n".join("- " + item for item in issues)
        try:
            identity = await SiteApiClient(
                self.config,
                transport=self.site_transport,
            ).resolve_identity(tg_uid)
        except SiteApiError:
            logger.error("[xingxy] 只读网站身份查询失败")
            return "网站身份查询暂不可用，请使用 /xy_status 检查配置后重试。"
        except Exception:
            logger.error("[xingxy] 只读网站身份查询遇到内部错误")
            return "网站身份查询暂不可用，请稍后重试。"

        user_id = identity["user_id"]
        return (
            "数据来源：Xingxy 网站（只读）\n"
            "TG UID：{tg_uid}\n绑定状态：{status}\n网站 user_id：{user_id}"
        ).format(
            tg_uid=tg_uid,
            status=identity["status"],
            user_id=user_id if user_id is not None else "未绑定",
        )

    def _site_command_tg_uid(self, event: AstrMessageEvent) -> Optional[str]:
        tg_uid = self._tg_uid(event)
        if (
            not tg_uid
            or not tg_uid.isascii()
            or not tg_uid.isdigit()
            or int(tg_uid) <= 0
            or int(tg_uid) > 9223372036854775807
        ):
            return None
        return tg_uid

    def _site_client(self) -> SiteApiClient:
        return SiteApiClient(self.config, transport=self.site_transport)

    def _points_site_config(self) -> Dict[str, Any]:
        config = dict(self.config)
        config["service_key_id"] = self.config.get("points_service_key_id", "")
        config["service_secret"] = self.config.get("points_service_secret", "")
        return config

    def _checkin_site_config(self) -> Dict[str, Any]:
        config = dict(self.config)
        config["service_key_id"] = self.config.get("checkin_service_key_id", "")
        config["service_secret"] = self.config.get("checkin_service_secret", "")
        return config

    async def _points_balance_text(self, event: AstrMessageEvent) -> str:
        if self.config.get("points_read_enabled", False) is not True:
            return "网站积分查询当前未启用。"
        if not self._is_telegram_private(event):
            return "网站积分查询仅接受 Telegram 私聊消息。"
        if self._trace_logging_enabled():
            return "为保护站点积分隐私，请管理员关闭 AstrBot 全局 trace_enable 后再查询。"
        tg_uid = self._site_command_tg_uid(event)
        if not tg_uid:
            return "无法读取有效的 Telegram 用户 ID。"
        config = self._points_site_config()
        if configuration_issues(config):
            return "网站积分读取凭据未就绪，请联系管理员。"
        try:
            result = await SiteApiClient(config, transport=self.site_transport).points_balance(tg_uid)
        except Exception:
            logger.error("[xingxy] 网站积分只读查询失败")
            return "网站积分查询暂不可用，请稍后重试。"
        if result["user_id"] is None:
            return "数据来源：星小芽网站（只读）\n当前 Telegram 账号未绑定站点，请先发送 /xy_bind。"
        ledger = "已启用" if result["ledger_enabled"] else "尚未启用"
        checkin = "已启用" if result["checkin_enabled"] else "尚未开放"
        return ("数据来源：星小芽网站（只读）\nTG UID：{tg_uid}\n网站 user_id：{user_id}\n"
                "站点积分余额：{balance}\n统一积分账本：{ledger}\nTG 真实积分签到：{checkin}").format(
                    tg_uid=tg_uid, user_id=result["user_id"], balance=result["balance"],
                    ledger=ledger, checkin=checkin)

    async def _site_checkin_text(
        self,
        event: AstrMessageEvent,
        event_id: str,
    ) -> str:
        if self.config.get("site_checkin_enabled", False) is not True:
            return "网站真实签到当前未启用。"
        if not self._is_telegram_private(event):
            return "网站签到仅接受 Telegram 私聊消息。"
        if self._trace_logging_enabled():
            return "为保护站点签到请求，请管理员关闭 AstrBot 全局 trace_enable 后再签到。"
        tg_uid = self._site_command_tg_uid(event)
        if not tg_uid:
            return "无法读取有效的 Telegram 用户 ID。"
        config = self._checkin_site_config()
        if configuration_issues(config):
            return "网站签到专用凭据未就绪，请联系管理员。"
        try:
            result = await SiteApiClient(
                config,
                transport=self.site_transport,
            ).checkin(tg_uid, event_id)
        except Exception:
            logger.error("[xingxy] 网站签到请求失败")
            return "网站签到结果暂时无法确认。请使用同一请求重试：/xy_checkin {event_id}".format(
                event_id=event_id
            )

        if result["outcome"] == "unbound":
            return "当前 Telegram 账号未绑定站点，或暂时无权签到，请先完成 /xy_bind。"
        if result["outcome"] == "already_checked_in":
            return "ℹ️ 网站显示今日已签到（{date}），本次未重复发放积分。event_id={event_id}".format(
                date=result["date"], event_id=event_id
            )
        message = "✅ 网站签到成功：+{amount} 积分".format(amount=result["amount"])
        # The site owns the actual experience amount. Report only a validated,
        # positive value and never infer a balance or cumulative level locally.
        if result.get("integral", 0) > 0:
            message += "，+{integral} 经验".format(integral=result["integral"])
        return "{message}（{date}，event_id={event_id}）".format(
            message=message, date=result["date"], event_id=event_id
        )

    def _binding_site_config(self) -> Dict[str, Any]:
        binding_config = dict(self.config)
        binding_config["service_key_id"] = self.config.get(
            "binding_service_key_id", ""
        )
        binding_config["service_secret"] = self.config.get(
            "binding_service_secret", ""
        )
        return binding_config

    def _binding_site_client(self) -> SiteApiClient:
        return SiteApiClient(
            self._binding_site_config(),
            transport=self.site_transport,
        )

    def _binding_configuration_issues(self) -> Tuple[str, ...]:
        replacements = {
            "service_key_id": "binding_service_key_id",
            "service_secret": "binding_service_secret",
        }
        return tuple(
            next(
                (
                    issue.replace(source, replacement)
                    for source, replacement in replacements.items()
                    if source in issue
                ),
                issue,
            )
            for issue in configuration_issues(self._binding_site_config())
        )

    async def _bind_start_text(self, event: AstrMessageEvent, reauthorize: bool = False) -> str:
        if not self._binding_enabled():
            return "网站账号绑定当前未启用。"
        if not self._is_telegram_private(event):
            return "网站账号绑定仅接受 Telegram 私聊消息。"
        if self._trace_logging_enabled():
            return "网站账号绑定当前已暂停；请联系管理员关闭 AstrBot trace logging 后重试。"
        tg_uid = self._site_command_tg_uid(event)
        if not tg_uid:
            return "无法读取有效的 Telegram 用户 ID。"
        if self._binding_configuration_issues():
            return "网站账号绑定尚未配置完整，请联系管理员。"
        if not reauthorize:
            try:
                identity = await SiteApiClient(
                    self.config,
                    transport=self.site_transport,
                ).resolve_identity(tg_uid)
            except SiteApiError:
                return "暂时无法确认当前绑定状态，请稍后重试。"
            except Exception:
                return "暂时无法确认当前绑定状态，请稍后重试。"
            if identity["status"] == "active":
                return (
                    "数据来源：Xingxy 网站\n"
                    "此 Telegram 账号已绑定网站 user_id：{user_id}。\n"
                    "如需重新验证，请发送 /xy_bind_verify。"
                ).format(user_id=identity["user_id"])
        try:
            result = await self._binding_site_client().start_link(tg_uid)
        except SiteApiError:
            logger.error("[xingxy] 网站账号绑定授权准备失败")
            return "暂时无法开始网站账号绑定，请稍后重试。"
        except Exception:
            logger.error("[xingxy] 网站账号绑定授权遇到内部错误")
            return "暂时无法开始网站账号绑定，请稍后重试。"

        if result["status"] == "already_bound":
            identity = result["identity"]
            return (
                "数据来源：Xingxy 网站\n"
                "此 Telegram 账号已绑定网站 user_id：{user_id}。"
            ).format(user_id=identity["user_id"])
        return (
            "数据来源：Xingxy 网站\n"
            "请在 10 分钟内打开此授权链接：\n{authorization_url}\n"
            "已有绑定也可以重新授权；网页授权本身不会更改绑定。完成授权后，"
            "返回本私聊发送页面显示的 /xy_bind_confirm <code> 确认码；"
            "原绑定只会在确认同一账号时保持不变，不会被其他网站账号自动替换。"
        ).format(authorization_url=result["authorization_url"])

    async def _bind_confirm_text(
        self,
        event: AstrMessageEvent,
        confirmation_code: str,
    ) -> str:
        if not self._binding_enabled():
            return "网站账号绑定当前未启用。"
        if not self._is_telegram_private(event):
            return "网站账号绑定仅接受 Telegram 私聊消息。"
        if self._trace_logging_enabled():
            return "网站账号绑定当前已暂停；请联系管理员关闭 AstrBot trace logging 后重试。"
        tg_uid = self._site_command_tg_uid(event)
        if not tg_uid:
            return "无法读取有效的 Telegram 用户 ID。"
        if self._binding_configuration_issues():
            return "网站账号绑定尚未配置完整，请联系管理员。"
        if not isinstance(confirmation_code, str) or not CONFIRMATION_CODE_RE.fullmatch(
            confirmation_code
        ):
            return "确认码格式无效；请使用网站页面显示的 16 位确认码。"
        try:
            identity = await self._binding_site_client().confirm_link(
                tg_uid,
                confirmation_code,
            )
        except SiteBindingConflictError:
            logger.error("[xingxy] 网站账号绑定确认存在冲突")
            return (
                "无法绑定：该站点账号或当前 Telegram 账号已存在其他绑定，原绑定不会被替换。\n"
                "如果该站点账号已绑定其他 Telegram，请切换到未绑定的站点账号，再发送 /xy_bind 重新授权。"
            )
        except SiteBindingConfirmationError:
            logger.error("[xingxy] 网站账号绑定确认被拒绝")
            return (
                "绑定确认未通过：链接可能已失效，或不属于当前 Telegram 账号，"
                "也可能当前账号暂不允许绑定。请发送 /xy_bind 重新授权；若仍失败，请联系管理员。"
            )
        except SiteApiError:
            logger.error("[xingxy] 网站账号绑定确认服务暂不可用")
            return "网站账号绑定未完成：服务暂时不可用，请稍后重试。"
        except Exception:
            logger.error("[xingxy] 网站账号绑定确认遇到内部错误")
            return "网站账号绑定未完成，请稍后重试。"
        return (
            "✅ 网站账号绑定成功\n"
            "数据来源：Xingxy 网站\n"
            "TG UID：{tg_uid}\n网站 user_id：{user_id}"
        ).format(tg_uid=identity["tg_uid"], user_id=identity["user_id"])

    async def _bind_deep_link_text(self, event: AstrMessageEvent) -> str:
        """Confirm a site-owned binding from a Telegram start deep link."""
        match = re.fullmatch(START_BIND_PATTERN, event.get_message_str().strip())
        if match is None:
            return ""
        if not self._enabled():
            return self._disabled_message()
        if not self._binding_enabled():
            return "网站账号绑定当前未启用。"
        if not self._is_telegram_private(event):
            return "网站账号绑定仅接受 Telegram 私聊消息。"
        return await self._bind_confirm_text(event, match.group(1))

    async def _admin_allowed(self, event: AstrMessageEvent) -> bool:
        if self.config.get("admin_only_reset", True) is False:
            return True
        checker = getattr(event, "is_admin", None)
        if not callable(checker):
            return False
        result = checker()
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)

    def _disabled_message(self):
        return "Xingxy 测试插件当前已禁用。"

    @filter.command("xy_help")
    async def cmd_help(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        if self._backend_mode() == "site":
            site_checkin_state = (
                "网站签到功能已启用。"
                if self.config.get("site_checkin_enabled", False) is True
                else "网站签到功能当前关闭。"
            )
        else:
            site_checkin_state = "local 模式签到只写入本地测试数据。"
        yield event.plain_result(
            "Xingxy 命令：\n"
            "/xy_help — 查看命令\n"
            "/xy_identity — 按 backend_mode 查看身份（site 模式为只读）\n"
            "/xy_site_identity — 只读查询网站身份（Telegram 私聊）\n"
            "/xy_balance — 只读查询真实站点积分（Telegram 私聊）\n"
            "/xy_bind — 首次申请网站授权；已绑定账号直接显示绑定状态\n"
            "/xy_bind_verify — 已绑定账号重新验证授权\n"
            "/xy_bind_confirm <code> — 授权后返回本私聊确认绑定\n"
            "/xy_status — 查看模式和网站 API 配置状态\n"
            "/xy_checkin [event_id] — 按 backend_mode 签到；可传 event_id 安全重试\n"
            "签到状态：{site_checkin_state}\n"
            "/xy_flow — 查看签到步骤与当前测试数据\n"
            "/xy_reset — 清空本地测试数据（默认仅管理员）".format(
                site_checkin_state=site_checkin_state
            )
        )

    @filter.command("xy_identity")
    async def cmd_identity(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        mode = self._backend_mode()
        if mode == "invalid":
            yield event.plain_result("backend_mode 配置无效；请设置为 local 或 site。")
            return
        if mode == "site":
            yield event.plain_result(await self._site_identity_text(event))
            return
        identity, error = self._local_identity(event)
        if error:
            yield event.plain_result(error)
            return
        user_id = identity["user_id"]
        balance = 0
        if user_id is not None:
            balance = self.store.load()["points"].get(str(user_id), 0)
        yield event.plain_result(
            "数据来源：AstrBot 本地模拟，不是站点余额\n"
            "TG UID：{tg_uid}\n绑定状态：{status}\n测试 user_id：{user_id}\n积分余额：{balance}".format(
                tg_uid=identity["tg_uid"],
                status=identity["status"],
                user_id=user_id if user_id is not None else "未绑定",
                balance=balance,
            )
        )

    @filter.command("xy_site_identity")
    async def cmd_site_identity(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        yield event.plain_result(await self._site_identity_text(event))

    @filter.command("xy_status")
    async def cmd_status(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        mode = self._backend_mode()
        mode_label = mode if mode != "invalid" else "无效（仅支持 local 或 site）"
        read_issues = configuration_issues(self.config)
        if read_issues:
            read_status = "身份读取 API 配置未就绪：\n" + "\n".join(
                "- " + item for item in read_issues
            )
        else:
            read_status = "身份读取 API 凭据完整。"
        binding_issues = self._binding_configuration_issues()
        if binding_issues:
            binding_credentials = "账号绑定 API 凭据未就绪：\n" + "\n".join(
                "- " + item for item in binding_issues
            )
        else:
            binding_credentials = "账号绑定 API 凭据完整。"
        binding_status = "已启用" if self._binding_enabled() else "已禁用"
        trace_enabled = self._trace_logging_enabled()
        trace_status = "已开启" if trace_enabled else "已关闭"
        if self._binding_enabled() and trace_enabled:
            binding_status += "（trace logging 已开启，绑定命令暂停）"
        points_status = "已启用" if self.config.get("points_read_enabled", False) is True else "未启用"
        points_ready = "未就绪" if configuration_issues(self._points_site_config()) else "完整"
        checkin_status = "已启用" if self.config.get("site_checkin_enabled", False) is True else "已禁用"
        checkin_ready = "未就绪" if configuration_issues(self._checkin_site_config()) else "完整"
        if self._backend_mode() == "site" and checkin_status == "已启用" and trace_enabled:
            checkin_status += "（trace logging 已开启，签到命令暂停）"
        yield event.plain_result(
            "运行模式：{mode}\n网站账号绑定：{binding}\nAstrBot trace logging：{trace}\n"
            "{read_status}\n{binding_credentials}\n站点积分查询：{points_status}；凭据{points_ready}\n"
            "网站签到：{checkin_status}；专用凭据{checkin_ready}\n此命令未执行联网检查。".format(
                mode=mode_label,
                binding=binding_status,
                trace=trace_status,
                read_status=read_status,
                binding_credentials=binding_credentials,
                points_status=points_status,
                points_ready=points_ready,
                checkin_status=checkin_status,
                checkin_ready=checkin_ready,
            )
        )

    @filter.command("xy_balance")
    async def cmd_balance(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        yield event.plain_result(await self._points_balance_text(event))

    @filter.command("xy_bind")
    async def cmd_bind(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        yield event.plain_result(await self._bind_start_text(event))

    @filter.command("xy_bind_verify")
    async def cmd_bind_verify(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        yield event.plain_result(await self._bind_start_text(event, reauthorize=True))

    @filter.command("xy_bind_confirm")
    async def cmd_bind_confirm(
        self,
        event: AstrMessageEvent,
        confirmation_code: str = "",
    ):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        yield event.plain_result(
            await self._bind_confirm_text(event, confirmation_code)
        )

    @filter.regex(START_BIND_PATTERN)
    async def cmd_bind_deep_link(self, event: AstrMessageEvent):
        result = await self._bind_deep_link_text(event)
        if result:
            yield event.plain_result(result)
            event.stop_event()

    @filter.command("xy_checkin")
    async def cmd_checkin(self, event: AstrMessageEvent, event_id: str = ""):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        mode = self._backend_mode()
        if mode == "site":
            if self.config.get("site_checkin_enabled", False) is not True:
                yield event.plain_result("网站真实签到当前未启用。")
                return
            event_id = (event_id or "").strip() or str(uuid.uuid4())
            if not EVENT_ID_RE.fullmatch(event_id):
                yield event.plain_result(
                    "event_id 格式无效；请使用 1–128 位 ASCII 字母、数字、点、下划线、冒号或连字符。"
                )
                return
            yield event.plain_result(await self._site_checkin_text(event, event_id))
            return
        if mode == "invalid":
            yield event.plain_result("backend_mode 配置无效；未执行签到。请设置为 local 或 site。")
            return
        tg_uid = self._tg_uid(event)
        if not tg_uid:
            yield event.plain_result("无法读取当前发送者的 TG UID。")
            return
        event_id = (event_id or "").strip() or "xy-{}".format(uuid.uuid4().hex)
        if not EVENT_ID_RE.fullmatch(event_id):
            yield event.plain_result(
                "event_id 格式无效；请使用 1–128 位 ASCII 字母、数字、点、下划线、冒号或连字符。"
            )
            return
        try:
            outcome, result = self.store.checkin(
                tg_uid=tg_uid,
                event_id=event_id,
                reward_points=self._reward_points(),
                default_bound=self._default_bound(),
                default_test_user_id=self._test_user_id(),
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.error("[xingxy] 保存本地测试签到数据失败：%s", exc)
            yield event.plain_result("本地测试数据暂时无法读写，请检查插件数据目录。")
            return

        if outcome == "success":
            message = "✅ 签到成功：+{reward} 积分，余额 {balance}（event_id={event_id}）".format(
                reward=result["reward_points"],
                balance=result["balance_after"],
                event_id=event_id,
            )
        elif outcome == "replay":
            message = "🔁 replay：event_id={event_id}，原结果={status}，日期={date}，余额={balance}".format(
                event_id=event_id,
                status=result["status"],
                date=result["date"],
                balance=result["balance_after"],
            )
        elif outcome == "already_checked_in":
            message = "ℹ️ already_checked_in：今天已签到（event_id={event_id}，首次 event_id={first}），余额={balance}".format(
                event_id=event_id,
                first=result["first_event_id"],
                balance=result["balance_after"],
            )
        elif outcome == "unbound":
            message = "⚠️ 当前 TG UID 未绑定测试 user_id，未发放积分。"
        else:
            message = "⚠️ event_id 已属于其他测试身份，未发放积分。"
        yield event.plain_result("数据来源：AstrBot 本地模拟\n" + message)

    @filter.command("xy_flow")
    async def cmd_flow(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        identity, error = self._local_identity(event)
        if error:
            yield event.plain_result(error)
            return
        state = self.store.load()
        user_id = identity["user_id"]
        date = shanghai_date()
        day_key = "{}:{}".format(user_id, date) if user_id is not None else None
        first_event_id = state["checkin_by_day"].get(day_key) if day_key else None
        balance = state["points"].get(str(user_id), 0) if user_id is not None else 0
        summary = self.store.data_summary()
        lines = [
            "数据来源：AstrBot 本地模拟，不是站点余额",
            "消息：/xy_flow",
            "身份解析：tg_uid={tg_uid}，status={status}，test_user_id={user_id}".format(
                tg_uid=identity["tg_uid"],
                status=identity["status"],
                user_id=user_id if user_id is not None else "未绑定",
            ),
            "事件幂等：按 event_id 去重；已记录事件数={}".format(summary["events"]),
            "签到日：Asia/Shanghai {date}；当日状态={status}".format(
                date=date,
                status="已签到（event_id={}）".format(first_event_id)
                if first_event_id
                else "未签到",
            ),
            "积分：余额={}；签到奖励={}（测试配置）".format(
                balance, self._reward_points()
            ),
            "响应：流程预览完成；未发起签到。",
            "当前数据摘要：身份记录={identities}，签到事件={events}，积分余额合计={total}".format(
                identities=summary["identities"],
                events=summary["events"],
                total=sum(summary["points"].values()),
            ),
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("xy_reset")
    async def cmd_reset(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result(self._disabled_message())
            return
        if not await self._admin_allowed(event):
            yield event.plain_result("此命令仅允许管理员清空 Xingxy 测试数据。")
            return
        try:
            self.store.reset()
        except (OSError, ValueError, TypeError) as exc:
            logger.error("[xingxy] 清空本地测试数据失败：%s", exc)
            yield event.plain_result("本地测试数据暂时无法清空，请检查插件数据目录。")
            return
        yield event.plain_result("✅ Xingxy 本地测试数据已清空。")
