import hashlib
import hmac
import time

TOLERANCE = 300  # seconds


def sign(secret: str, timestamp: int, body: bytes) -> str:
    msg = f"{timestamp}.".encode() + body
    return "v1=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp, body: bytes, signature: str) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > TOLERANCE:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature or "")
