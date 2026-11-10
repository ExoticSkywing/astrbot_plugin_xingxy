"""Request signature codec; nonce storage and request authorization are external."""

import hashlib
import hmac
import re


_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TIMESTAMP = re.compile(r"[1-9][0-9]{9}\Z")
_NONCE = re.compile(r"[a-f0-9]{32}\Z")
_ROUTE = re.compile(r"/xingxy-ecosystem/v1/[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*\Z")
_SIGNATURE = re.compile(r"[a-f0-9]{64}\Z")
_VERSION = b"XINGXY-HMAC-SHA256-V1"


def _validate(key_id, timestamp, nonce, method, route, body):
    if not all(type(value) is str for value in (key_id, timestamp, nonce, method, route)):
        raise ValueError("Metadata must be strings.")
    if type(body) is not bytes:
        raise ValueError("Body must be bytes.")
    if not (_KEY_ID.fullmatch(key_id) and _TIMESTAMP.fullmatch(timestamp)
            and _NONCE.fullmatch(nonce) and method == "POST" and _ROUTE.fullmatch(route)):
        raise ValueError("Invalid signature metadata.")


def canonical_message(key_id, timestamp, nonce, method, route, body):
    """Return canonical request bytes for the exact raw body."""
    _validate(key_id, timestamp, nonce, method, route, body)
    fields = (_VERSION, key_id.encode("ascii"), timestamp.encode("ascii"),
              nonce.encode("ascii"), b"POST", route.encode("ascii"),
              hashlib.sha256(body).hexdigest().encode("ascii"))
    return b"\n".join(fields)


def sign(secret, key_id, timestamp, nonce, method, route, body):
    """Return a lowercase hexadecimal HMAC-SHA256 signature."""
    if type(secret) is not bytes or len(secret) < 32:
        raise ValueError("Secret must be bytes of at least 32 bytes.")
    message = canonical_message(key_id, timestamp, nonce, method, route, body)
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify(secret, key_id, timestamp, nonce, method, route, body, signature, now, max_skew=300):
    """Check signature and time window; nonce storage and authorization are external."""
    if (not isinstance(now, int) or isinstance(now, bool) or now < 0
            or not isinstance(max_skew, int) or isinstance(max_skew, bool)
            or max_skew < 0 or max_skew > 300):
        return False
    if type(signature) is not str or not _SIGNATURE.fullmatch(signature):
        return False
    try:
        if not _TIMESTAMP.fullmatch(timestamp) or abs(now - int(timestamp)) > max_skew:
            return False
        expected = sign(secret, key_id, timestamp, nonce, method, route, body)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)
