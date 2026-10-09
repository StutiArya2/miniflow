"""Alerts, cloud storage and AI: webhook, slack_message, teams_message, llm_prompt, s3_get, s3_put.

Webhook URLs, API keys and cloud credentials are secrets: they must come from
``{{ env.NAME }}`` and are hidden as ``••••`` in errors.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from miniflow.blocks import Setting, block
from miniflow.blocks.builtin.data import http_request
from miniflow.blocks.sigv4 import sign_request


# ------------------------------------------------------------------- alerts
@block(
    "webhook",
    settings=[
        Setting("url", str, help="Address to send to"),
        Setting("body", object, default=None, help="Sent as JSON (object or list) or as text"),
        Setting("method", str, default="POST", choices=("POST", "PUT", "PATCH")),
        Setting("headers", dict, default=None),
        Setting("timeout", float, default=30.0),
    ],
)
def webhook(
    url: str,
    body: Any = None,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Send data to a web address (any service that accepts a webhook); fails on 4xx/5xx."""
    reply: dict[str, Any] = http_request(
        url=url, method=method, headers=headers, body=body, timeout=timeout
    )
    return reply


def _post_alert(service: str, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    reply = http_request(url=url, method="POST", body=payload, fail_on_error=False, timeout=timeout)
    if reply["status"] >= 400:
        raise RuntimeError(
            f"{service} refused the message ({reply['status']}): {reply['text'][:200].strip()}"
        )
    return {"status": reply["status"], "response": reply["text"]}


@block(
    "slack_message",
    settings=[
        Setting(
            "webhook_url",
            str,
            secret=True,
            help="Slack incoming-webhook URL: {{ env.SLACK_WEBHOOK }}",
        ),
        Setting("text", str, help="The message"),
        Setting(
            "username", str, default="", help="Name shown as the sender (if the webhook allows it)"
        ),
        Setting("timeout", float, default=30.0),
    ],
)
def slack_message(
    webhook_url: str, text: str, username: str = "", timeout: float = 30.0
) -> dict[str, Any]:
    """Post a message to Slack through an incoming webhook."""
    payload: dict[str, Any] = {"text": text}
    if username:
        payload["username"] = username
    return _post_alert("Slack", webhook_url, payload, timeout)


@block(
    "teams_message",
    settings=[
        Setting(
            "webhook_url",
            str,
            secret=True,
            help="Teams workflow webhook URL: {{ env.TEAMS_WEBHOOK }}",
        ),
        Setting("text", str, help="The message"),
        Setting("title", str, default="", help="Bold heading above the message"),
        Setting("timeout", float, default=30.0),
    ],
)
def teams_message(
    webhook_url: str, text: str, title: str = "", timeout: float = 30.0
) -> dict[str, Any]:
    """Post a message to a Microsoft Teams channel (as an adaptive card) through a webhook."""
    body: list[dict[str, Any]] = []
    if title:
        body.append(
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}
        )
    body.append({"type": "TextBlock", "text": text, "wrap": True})
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
    payload = {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }
    return _post_alert("Teams", webhook_url, payload, timeout)


# ---------------------------------------------------------------------- AI
_LLM_DEFAULTS = {
    "anthropic": ("https://api.anthropic.com", "claude-sonnet-5-5"),
    "openai": ("https://api.openai.com", "gpt-4o-mini"),
}


@block(
    "llm_prompt",
    settings=[
        Setting(
            "prompt", str, help="What to ask; build it from earlier steps with {{ step.field }}"
        ),
        Setting("api_key", str, secret=True, help="{{ env.ANTHROPIC_API_KEY }}"),
        Setting(
            "provider", str, default="anthropic", choices=tuple(_LLM_DEFAULTS), help="API style"
        ),
        Setting(
            "model", str, default="", help="Model name (default: a current one for the provider)"
        ),
        Setting("system", str, default="", help="Standing instructions for the model"),
        Setting("max_tokens", int, default=1024),
        Setting(
            "base_url",
            str,
            default="",
            help="Another server with the same API (default: the provider's)",
        ),
        Setting("timeout", float, default=120.0),
    ],
)
def llm_prompt(
    prompt: str,
    api_key: str,
    provider: str = "anthropic",
    model: str = "",
    system: str = "",
    max_tokens: int = 1024,
    base_url: str = "",
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Ask an AI model (Anthropic, or an OpenAI-compatible API) and return its text answer."""
    default_url, default_model = _LLM_DEFAULTS[provider]
    root = (base_url or default_url).rstrip("/")
    model = model or default_model
    if provider == "anthropic":
        url = f"{root}/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
    else:
        url = f"{root}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    reply = http_request(
        url=url, method="POST", headers=headers, body=body, fail_on_error=False, timeout=timeout
    )
    data = reply["json"] if isinstance(reply["json"], dict) else {}
    if reply["status"] >= 400:
        error = data.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        message = str(detail or reply["text"][:200]).replace(api_key, "••••")
        raise RuntimeError(f"the {provider} API returned {reply['status']}: {message}")
    if provider == "anthropic":
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
    else:
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    return {"text": text, "model": data.get("model", model), "usage": data.get("usage", {})}


# ----------------------------------------------------------------------- S3
_S3_CREDENTIALS = [
    Setting("bucket", str),
    Setting("key", str, help="Object name, e.g. reports/2026/sales.csv"),
    Setting("access_key", str, secret=True, help="{{ env.AWS_ACCESS_KEY_ID }}"),
    Setting("secret_key", str, secret=True, help="{{ env.AWS_SECRET_ACCESS_KEY }}"),
    Setting("session_token", str, default="", secret=True, help="For temporary credentials"),
    Setting("region", str, default="us-east-1"),
    Setting(
        "endpoint", str, default="", help="Another S3-compatible server, e.g. MinIO (default: AWS)"
    ),
    Setting("timeout", float, default=120.0),
]


def _s3_url(bucket: str, key: str, region: str, endpoint: str) -> str:
    path = quote(key.lstrip("/"), safe="/-_.~")
    if endpoint:  # custom servers use path-style addresses
        return f"{endpoint.rstrip('/')}/{bucket}/{path}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{path}"


def _s3(method: str, url: str, payload: bytes, cfg: dict[str, Any], what: str) -> bytes:
    headers: dict[str, str] = {}
    if cfg["session_token"]:
        headers["x-amz-security-token"] = cfg["session_token"]
    signed = sign_request(
        method, url, headers, payload, cfg["access_key"], cfg["secret_key"], cfg["region"]
    )
    request = urllib.request.Request(
        url, data=payload if method == "PUT" else None, headers=signed, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg["timeout"]) as response:
            data: bytes = response.read()
            return data
    except urllib.error.HTTPError as err:
        text = err.read().decode(errors="replace")
        code = re.search(r"<Code>([^<]+)</Code>", text)
        raise RuntimeError(
            f"S3 {what} failed: {err.code} {code.group(1) if code else err.reason}"
        ) from None


@block(
    "s3_get",
    settings=[
        Setting("path", str, file=True, help="Where to save the file; folders are created"),
        *_S3_CREDENTIALS,
    ],
)
def s3_get(
    path: str,
    bucket: str,
    key: str,
    access_key: str,
    secret_key: str,
    session_token: str = "",
    region: str = "us-east-1",
    endpoint: str = "",
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Download an object from S3 (or an S3-compatible server) to a local file."""
    cfg = {
        "access_key": access_key,
        "secret_key": secret_key,
        "session_token": session_token,
        "region": region,
        "timeout": timeout,
    }
    data = _s3(
        "GET", _s3_url(bucket, key, region, endpoint), b"", cfg, f"download of {bucket}/{key}"
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"bucket": bucket, "key": key, "bytes": len(data), "path": str(target)}


@block(
    "s3_put",
    settings=[
        Setting("path", str, default="", file=True, help="Local file to upload (or give content)"),
        Setting("content", str, default="", help="Text to upload instead of a file"),
        *_S3_CREDENTIALS,
    ],
)
def s3_put(
    bucket: str,
    key: str,
    access_key: str,
    secret_key: str,
    path: str = "",
    content: str = "",
    session_token: str = "",
    region: str = "us-east-1",
    endpoint: str = "",
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Upload a file (or some text) to S3 or an S3-compatible server."""
    if bool(path) == bool(content):
        raise ValueError("give either path or content, not both and not neither")
    data = Path(path).read_bytes() if path else content.encode()
    cfg = {
        "access_key": access_key,
        "secret_key": secret_key,
        "session_token": session_token,
        "region": region,
        "timeout": timeout,
    }
    _s3("PUT", _s3_url(bucket, key, region, endpoint), data, cfg, f"upload to {bucket}/{key}")
    return {"bucket": bucket, "key": key, "bytes": len(data)}
