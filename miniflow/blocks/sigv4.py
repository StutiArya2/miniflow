"""AWS Signature Version 4 request signing, standard library only.

Used by the ``s3_get`` and ``s3_put`` blocks. :func:`sign_request` returns the headers
to send (the ones given, plus ``Authorization``, ``x-amz-date`` and
``x-amz-content-sha256``). The tests check it against the worked example in AWS's
documentation, the signature of which was also reproduced with AWS's own library.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlsplit

__all__ = ["sign_request"]

_ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def _canonical_query(query: str) -> str:
    pairs = sorted(
        (quote(k, safe="-_.~"), quote(v, safe="-_.~"))
        for k, v in parse_qsl(query, keep_blank_values=True)
    )
    return "&".join(f"{k}={v}" for k, v in pairs)


def sign_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    now: datetime | None = None,
) -> dict[str, str]:
    """Headers for ``method url`` signed with AWS Signature V4 (the payload hash is signed)."""
    parts = urlsplit(url)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    payload_hash = hashlib.sha256(payload).hexdigest()

    out = dict(headers)
    out["x-amz-date"] = stamp
    out["x-amz-content-sha256"] = payload_hash

    signed = {name.lower().strip(): " ".join(str(value).split()) for name, value in out.items()}
    signed["host"] = parts.netloc
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)

    canonical = "\n".join(
        [
            method.upper(),
            quote(parts.path or "/", safe="/-_.~"),
            _canonical_query(parts.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join([_ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    key = _hmac(("AWS4" + secret_key).encode(), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

    out["Authorization"] = (
        f"{_ALGORITHM} Credential={access_key}/{scope},"
        f"SignedHeaders={signed_headers},Signature={signature}"
    )
    return out
