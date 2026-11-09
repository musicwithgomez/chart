"""Ed25519 signatures in pure Python (RFC 8032), no extra packages needed.

The license server signs every license answer with its PRIVATE seed; the bridge checks it with the PUBLIC key.
A customer can therefore not fake "my subscription is active": they would need the private seed, which never
leaves your license server.

    seed = new_seed()                     # 32 random bytes, keep secret (license server only)
    pub  = public_key(seed)               # 32 bytes, safe to ship inside the bridge
    tok  = sign_token({"plan": "pro"}, seed)
    data = verify_token(tok, pub)         # dict, or raises ValueError
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

_p = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _inv(x: int) -> int:
    return pow(x, _p - 2, _p)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = 4 * _inv(5) % _p
_Bx = _xrecover(_By)
_B = (_Bx, _By, 1, _Bx * _By % _p)  # extended coordinates (X, Y, Z, T)
_ZERO = (0, 1, 1, 0)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _p
    b = (y1 + x1) * (y2 + x2) % _p
    c = 2 * t1 * t2 * _d % _p
    dd = 2 * z1 * z2 % _p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _p, g * h % _p, f * g % _p, e * h % _p)


def _mul(s: int, P):
    Q = _ZERO
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _encode(P) -> bytes:
    x, y, z, _ = P
    zi = _inv(z)
    x, y = x * zi % _p, y * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode(s: bytes):
    if len(s) != 32:
        raise ValueError("bad point")
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    if y >= _p:
        raise ValueError("bad point")
    x = _xrecover(y)
    if (x & 1) != (s[31] >> 7):
        x = _p - x
    P = (x, y, 1, x * y % _p)
    # on-curve check: -x^2 + y^2 = 1 + d x^2 y^2
    if (-x * x + y * y - 1 - _d * x * x * y * y) % _p != 0:
        raise ValueError("point not on curve")
    return P


def _h(m: bytes) -> int:
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def _expand(seed: bytes):
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def new_seed() -> bytes:
    return os.urandom(32)


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _encode(_mul(a, _B))


def sign(msg: bytes, seed: bytes) -> bytes:
    a, prefix = _expand(seed)
    A = _encode(_mul(a, _B))
    r = _h(prefix + msg) % _L
    R = _encode(_mul(r, _B))
    k = _h(R + A + msg) % _L
    S = (r + k * a) % _L
    return R + int.to_bytes(S, 32, "little")


def verify(msg: bytes, sig: bytes, pub: bytes) -> bool:
    try:
        if len(sig) != 64:
            return False
        A = _decode(pub)
        R = _decode(sig[:32])
        S = int.from_bytes(sig[32:], "little")
        if S >= _L:
            return False
        k = _h(sig[:32] + pub + msg) % _L
        left = _encode(_mul(S, _B))
        right = _encode(_add(R, _mul(k, A)))
        return left == right
    except ValueError:
        return False


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_token(payload: dict, seed: bytes) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return b64(body) + "." + b64(sign(body, seed))


def verify_token(token: str, pub: bytes) -> dict:
    try:
        body_s, sig_s = token.split(".", 1)
        body, sig = unb64(body_s), unb64(sig_s)
    except Exception:
        raise ValueError("Malformed license token.")
    if not verify(body, sig, pub):
        raise ValueError("License token signature is invalid.")
    return json.loads(body)
