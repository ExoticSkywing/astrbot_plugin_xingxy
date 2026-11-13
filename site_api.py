"""Read-only client for the Xingxy site service API."""

import asyncio
import inspect
import json
import re
import secrets
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from . import service_signature


IDENTITY_ROUTE = "/xingxy-ecosystem/v1/identity/resolve"
LINK_START_ROUTE = "/xingxy-ecosystem/v1/identity/link/start"
IDENTITY_BIND_ROUTE = "/xingxy-ecosystem/v1/identity/bind"
POINTS_BALANCE_ROUTE = "/xingxy-ecosystem/v1/points/balance"
CHECKIN_ROUTE = "/xingxy-ecosystem/v1/tasks/checkin"
OAUTH_AUTHORIZE_PATH = "/wp-json/zibll-oauth/v1/authorize"
OAUTH_CALLBACK_PATH = "/wp-json/xingxy-ecosystem/v1/identity/link/callback"
CONFIRMATION_CODE_RE = re.compile(r"[A-Za-z0-9_-]{16}\Z")
STATE_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
CODE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
MAX_RESPONSE_BYTES = 65536


class SiteApiError(Exception):
    """A safe error category that never includes credentials or response data."""


class SiteBindingConflictError(SiteApiError):
    """The site rejected a binding because its binding state conflicts."""

    def __init__(self):
        super().__init__("site_api_binding_conflict")


class SiteBindingConfirmationError(SiteApiError):
    """The site denied confirmation without exposing its private reason."""

    def __init__(self):
        super().__init__("site_api_binding_confirmation_denied")


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except ValueError:
        return False


def configuration_issues(config: Dict[str, Any]) -> Tuple[str, ...]:
    """Return missing or invalid site settings without exposing their values."""
    issues = []
    base_url = config.get("site_base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        issues.append("site_base_url 未配置")
    else:
        try:
            parts = urlsplit(base_url.strip())
            valid_base_url = (
                parts.scheme == "https"
                and bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and not parts.query
                and not parts.fragment
                and parts.path in ("", "/")
            )
            # Accessing port validates malformed port text such as ``:bad``.
            _ = parts.port
        except ValueError:
            valid_base_url = False
        if not valid_base_url:
            issues.append("site_base_url 无效（需为 HTTPS 基础地址）")

    key_id = config.get("service_key_id")
    if not isinstance(key_id, str) or not key_id:
        issues.append("service_key_id 未配置")
    elif (
        not all(char.isascii() and (char.isalnum() or char in "_-") for char in key_id)
        or not (1 <= len(key_id) <= 64)
    ):
        issues.append("service_key_id 格式无效")

    secret = config.get("service_secret")
    if not isinstance(secret, str) or not secret:
        issues.append("service_secret 未配置")
    elif len(secret.encode("utf-8")) < 32:
        issues.append("service_secret 长度无效（至少 32 字节）")
    return tuple(issues)


async def _http_post(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, bytes]:
    """Post JSON with a total timeout and without forwarding signed redirects."""
    # AstrBot ships aiohttp. Lazy import keeps offline plugin tests independent
    # from the runtime package when they inject a stub transport.
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            data=body,
            headers=headers,
            allow_redirects=False,
        ) as response:
            chunks = []
            total_bytes = 0
            while total_bytes <= MAX_RESPONSE_BYTES:
                chunk = await response.content.read(
                    min(8192, MAX_RESPONSE_BYTES + 1 - total_bytes)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > MAX_RESPONSE_BYTES:
                    break
            response_body = b"".join(chunks)
            return response.status, response_body


class SiteApiClient:
    """Minimal HMAC client whose transport can be replaced in offline tests."""

    def __init__(
        self,
        config: Dict[str, Any],
        transport: Optional[Callable[[str, bytes, Dict[str, str]], Any]] = None,
    ):
        self.config = config
        self.transport = transport or _http_post

    def _request_parts(
        self,
        route: str,
        payload: Dict[str, str],
    ) -> Tuple[str, bytes, Dict[str, str]]:
        issues = configuration_issues(self.config)
        if issues:
            raise SiteApiError("site_api_configuration_invalid")

        base_url = self.config["site_base_url"].strip().rstrip("/")
        url = base_url + "/wp-json" + route
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        key_id = self.config["service_key_id"]
        secret = self.config["service_secret"].encode("utf-8")
        signature = service_signature.sign(
            secret,
            key_id,
            timestamp,
            nonce,
            "POST",
            route,
            body,
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Xingxy-Key-Id": key_id,
            "X-Xingxy-Timestamp": timestamp,
            "X-Xingxy-Nonce": nonce,
            "X-Xingxy-Signature": signature,
        }
        return url, body, headers

    async def _post_json(self, route: str, payload: Dict[str, str]) -> Any:
        _status, parsed = await self._post_json_response(route, payload, (200,))
        return parsed

    async def _post_json_response(
        self,
        route: str,
        payload: Dict[str, str],
        accepted_statuses: Tuple[int, ...],
    ) -> Tuple[int, Any]:
        try:
            url, body, headers = self._request_parts(route, payload)
            if inspect.iscoroutinefunction(self.transport):
                response = await self.transport(url, body, headers)
            else:
                response = await asyncio.to_thread(self.transport, url, body, headers)
                if inspect.isawaitable(response):
                    response = await response
            status, response_body = response
        except SiteApiError:
            raise
        except Exception as error:
            raise SiteApiError("site_api_unavailable") from error

        if not isinstance(status, int) or status not in accepted_statuses:
            raise SiteApiError("site_api_http_error")
        if not isinstance(response_body, bytes) or len(response_body) > MAX_RESPONSE_BYTES:
            raise SiteApiError("site_api_response_invalid")
        # A 403 deliberately has no parsed/displayable error body. It can mean
        # unbound or another policy denial, and the client must not disclose
        # server diagnostics or infer allowlist state from it.
        if status == 403:
            return status, None
        try:
            return status, json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            raise SiteApiError("site_api_response_invalid")

    async def resolve_identity(self, tg_uid: str) -> Dict[str, Any]:
        """Resolve one Telegram ID through the read-only site endpoint."""
        payload = await self._post_json(IDENTITY_ROUTE, {"tg_uid": tg_uid})
        return validate_identity_response(payload, tg_uid)

    async def points_balance(self, tg_uid: str) -> Dict[str, Any]:
        payload = await self._post_json(POINTS_BALANCE_ROUTE, {"tg_uid": tg_uid})
        identity = validate_identity_response(payload, tg_uid)
        if (payload.get("source") != "wordpress"
                or type(payload.get("ledger_enabled")) is not bool
                or type(payload.get("checkin_enabled")) is not bool):
            raise SiteApiError("site_api_response_invalid")
        balance = payload.get("balance")
        if identity["user_id"] is None:
            if balance is not None:
                raise SiteApiError("site_api_response_invalid")
        elif type(balance) is not int or not 0 <= balance <= 9007199254740991:
            raise SiteApiError("site_api_response_invalid")
        return dict(identity, balance=balance, ledger_enabled=payload["ledger_enabled"],
                    checkin_enabled=payload["checkin_enabled"])

    async def checkin(self, tg_uid: str, event_id: str) -> Dict[str, Any]:
        """Request one site check-in and validate its complete outcome."""
        if (
            not isinstance(tg_uid, str)
            or not tg_uid.isascii()
            or not tg_uid.isdigit()
            or int(tg_uid) <= 0
            or int(tg_uid) > 9223372036854775807
        ):
            raise SiteApiError("site_api_identity_invalid")
        if (
            not isinstance(event_id, str)
            or not event_id.isascii()
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", event_id)
        ):
            raise SiteApiError("site_api_event_id_invalid")

        status, payload = await self._post_json_response(
            CHECKIN_ROUTE,
            {"event_id": event_id, "tg_uid": tg_uid},
            (200, 403, 409),
        )
        if status == 403:
            return {"outcome": "unbound"}
        if status == 200:
            if not isinstance(payload, dict) or set(payload) - {"integral"} != {
                "ok", "checked_in", "date", "amount"
            }:
                raise SiteApiError("site_api_response_invalid")
            if (
                payload.get("ok") is not True
                or payload.get("checked_in") is not True
                or not _is_iso_date(payload.get("date"))
                or type(payload.get("amount")) is not int
                or not 0 <= payload["amount"] <= 9007199254740991
            ):
                raise SiteApiError("site_api_response_invalid")
            result = {
                "outcome": "success",
                "date": payload["date"],
                "amount": payload["amount"],
            }
            if "integral" in payload and (
                type(payload["integral"]) is not int
                or not 0 <= payload["integral"] <= 2147483647
            ):
                raise SiteApiError("site_api_response_invalid")
            if "integral" in payload:
                result["integral"] = payload["integral"]
            return result

        if not isinstance(payload, dict) or set(payload) != {
            "ok", "checked_in", "reason", "date", "amount"
        }:
            raise SiteApiError("site_api_response_invalid")
        if (
            payload.get("ok") is not False
            or payload.get("checked_in") is not False
            or payload.get("reason") != "already_checked_in"
            or not _is_iso_date(payload.get("date"))
            or type(payload.get("amount")) is not int
            or payload["amount"] != 0
        ):
            raise SiteApiError("site_api_response_invalid")
        return {"outcome": "already_checked_in", "date": payload["date"], "amount": 0}

    async def start_link(self, tg_uid: str) -> Dict[str, Any]:
        """Start one site-owned account-link session."""
        payload = await self._post_json(LINK_START_ROUTE, {"tg_uid": tg_uid})
        return validate_link_start_response(payload, tg_uid, self.config["site_base_url"])

    async def confirm_link(
        self,
        tg_uid: str,
        confirmation_code: str,
    ) -> Dict[str, Any]:
        """Confirm one site-owned account-link session."""
        if not isinstance(confirmation_code, str) or not CONFIRMATION_CODE_RE.fullmatch(confirmation_code):
            raise SiteApiError("site_api_confirmation_invalid")
        status, payload = await self._post_json_response(
            IDENTITY_BIND_ROUTE,
            {"tg_uid": tg_uid, "confirmation_code": confirmation_code},
            (200, 403, 409),
        )
        if status == 403:
            raise SiteBindingConfirmationError()
        if status == 409:
            if (
                isinstance(payload, dict)
                and set(payload) == {"ok", "error"}
                and payload.get("ok") is False
                and payload.get("error") == "conflict"
            ):
                raise SiteBindingConflictError()
            raise SiteApiError("site_api_response_invalid")
        identity = validate_identity_response(payload, tg_uid)
        if identity["user_id"] is None or identity["status"] != "active":
            raise SiteApiError("site_api_response_invalid")
        return identity


def validate_identity_response(payload: Any, tg_uid: str) -> Dict[str, Any]:
    """Validate the public identity response before the plugin displays it."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SiteApiError("site_api_response_invalid")
    bound = payload.get("bound")
    if type(bound) is not bool:
        raise SiteApiError("site_api_response_invalid")

    user = payload.get("user")
    if not bound:
        if user is not None:
            raise SiteApiError("site_api_response_invalid")
        return {"tg_uid": tg_uid, "user_id": None, "status": "unbound"}

    if not isinstance(user, dict):
        raise SiteApiError("site_api_response_invalid")
    user_id = user.get("user_id")
    response_tg_uid = user.get("tg_uid")
    status = user.get("status")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not isinstance(response_tg_uid, str)
        or response_tg_uid != tg_uid
        or status != "active"
    ):
        raise SiteApiError("site_api_response_invalid")
    return {"tg_uid": response_tg_uid, "user_id": user_id, "status": status}


def _origin(url_parts) -> Tuple[str, str, int]:
    """Return a normalized HTTPS origin tuple after validating its port."""
    try:
        port = url_parts.port or 443
    except ValueError:
        raise SiteApiError("site_api_response_invalid")
    hostname = url_parts.hostname
    if not hostname:
        raise SiteApiError("site_api_response_invalid")
    return url_parts.scheme.lower(), hostname.lower(), port


def validate_link_start_response(
    payload: Any,
    tg_uid: str,
    site_base_url: str,
) -> Dict[str, Any]:
    """Validate an already-bound result or the OAuth URL returned by the site."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SiteApiError("site_api_response_invalid")

    status = payload.get("status")
    if status == "already_bound":
        if payload.get("bound") is not True:
            raise SiteApiError("site_api_response_invalid")
        identity = validate_identity_response(
            {"ok": True, "bound": True, "user": payload.get("user")},
            tg_uid,
        )
        return {"status": status, "identity": identity}

    if status != "authorization_required":
        raise SiteApiError("site_api_response_invalid")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not (1 <= expires_in <= 600):
        raise SiteApiError("site_api_response_invalid")
    authorization_url = payload.get("authorization_url")
    if not isinstance(authorization_url, str) or not authorization_url:
        raise SiteApiError("site_api_response_invalid")
    try:
        parts = urlsplit(authorization_url)
        base_parts = urlsplit(site_base_url.strip())
        # Access .port during origin validation to reject malformed port text.
        same_origin = _origin(parts) == _origin(base_parts)
        query_items = parse_qsl(
            parts.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except (TypeError, ValueError):
        raise SiteApiError("site_api_response_invalid")
    if (
        parts.scheme.lower() != "https"
        or not same_origin
        or parts.username is not None
        or parts.password is not None
        or parts.path != OAUTH_AUTHORIZE_PATH
        or "#" in authorization_url
        or parts.fragment
    ):
        raise SiteApiError("site_api_response_invalid")

    query = {}
    allowed = {
        "client_id",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
    }
    for key, value in query_items:
        if key not in allowed or key in query:
            raise SiteApiError("site_api_response_invalid")
        query[key] = value
    required = allowed
    if (
        set(query) != required
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", query["client_id"])
        or query["redirect_uri"] != _origin_url(site_base_url) + OAUTH_CALLBACK_PATH
        or query["response_type"] != "code"
        or query["scope"] != "basic"
        or not STATE_RE.fullmatch(query["state"])
        or not CODE_CHALLENGE_RE.fullmatch(query["code_challenge"])
        or query["code_challenge_method"] != "S256"
    ):
        raise SiteApiError("site_api_response_invalid")

    return {
        "status": status,
        "authorization_url": authorization_url,
        "expires_in": expires_in,
    }


def _origin_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme, hostname, port = _origin(parts)
    host = "[{}]".format(hostname) if ":" in hostname else hostname
    port_suffix = "" if port == 443 else ":{}".format(port)
    return "{}://{}{}".format(scheme, host, port_suffix)
