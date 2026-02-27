from __future__ import annotations

import os
import time


_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, length: int) -> str:
    chars: list[str] = ["0"] * length
    for index in range(length - 1, -1, -1):
        value, mod = divmod(value, 32)
        chars[index] = _CROCKFORD_BASE32[mod]
    return "".join(chars)


def new_ulid() -> str:
    """Generate a ULID-compatible identifier without third-party deps."""
    timestamp_ms = int(time.time() * 1000)
    entropy_int = int.from_bytes(os.urandom(10), byteorder="big")
    return f"{_encode_base32(timestamp_ms, 10)}{_encode_base32(entropy_int, 16)}"

