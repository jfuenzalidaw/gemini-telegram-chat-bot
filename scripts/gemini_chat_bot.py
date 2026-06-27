#!/usr/bin/env python3
"""Telegram-controlled Gemini chat bot for GitHub Actions polling."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


DEFAULT_CONFIG_PATH = "config/gemini_chat.json"
DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_DAILY_REQUEST_LIMIT = 250
DEFAULT_GOOGLE_CLOUD_PROJECT_ID = "gen-lang-client-0857616622"
DEFAULT_GOOGLE_QUOTA_PROJECT_ID = "operations-499021"
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a concise Telegram assistant. Answer directly and ask a clarifying "
    "question only when needed."
)
QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
MONITORING_TIME_SERIES_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"
MODEL_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
TELEGRAM_MESSAGE_LIMIT = 3900
PROJECT_QUOTA_DAYS = 28
UNLIMITED_QUOTA_VALUE = 9_000_000_000_000_000_000
PROJECT_QUOTA_METRICS = {
    "request_usage": "generativelanguage.googleapis.com/quota/generate_content_free_tier_requests/usage",
    "request_limit": "generativelanguage.googleapis.com/quota/generate_content_free_tier_requests/limit",
    "token_usage": (
        "generativelanguage.googleapis.com/quota/"
        "generate_content_free_tier_input_token_count/usage"
    ),
    "token_limit": (
        "generativelanguage.googleapis.com/quota/"
        "generate_content_free_tier_input_token_count/limit"
    ),
}
LIMIT_NAME_TO_QUOTA_KIND = {
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier": "rpm",
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier": "rpd",
    "GenerateContentInputTokensPerModelPerMinute-FreeTier": "tpm",
}


@dataclass
class GeminiUsage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class GeminiResponse:
    text: str
    usage: GeminiUsage


@dataclass
class ProjectQuotaStat:
    model: str
    rpm_usage: int | None = None
    rpm_limit: int | None = None
    tpm_usage: int | None = None
    tpm_limit: int | None = None
    rpd_usage: int | None = None
    rpd_limit: int | None = None


@dataclass
class ChatConfig:
    telegram_update_offset: int | None = None
    enabled: bool = True
    model: str = DEFAULT_MODEL
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION
    google_cloud_project_id: str = DEFAULT_GOOGLE_CLOUD_PROJECT_ID
    google_quota_project_id: str | None = DEFAULT_GOOGLE_QUOTA_PROJECT_ID
    daily_request_limit: int = DEFAULT_DAILY_REQUEST_LIMIT
    quota_date: str | None = None
    quota_requests: int = 0
    quota_prompt_tokens: int = 0
    quota_output_tokens: int = 0
    quota_total_tokens: int = 0


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def normalize_model(raw_model: str) -> str:
    model = raw_model.strip()
    if model.startswith("models/"):
        model = model.removeprefix("models/")
    if not MODEL_PATTERN.fullmatch(model):
        raise ValueError("Use a Gemini model id like gemini-2.0-flash.")
    return model


def clean_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def base64url(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def fetch_json(
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {
        "User-Agent": "telegram-gemini-chat-bot/1.0",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_api(token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    if params is not None:
        data = urllib.parse.urlencode(params).encode("utf-8")
    response = fetch_json(
        TELEGRAM_API_URL.format(token=token, method=method),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not response.get("ok"):
        raise RuntimeError(f"Telegram API call {method} failed: {response}")
    return response


def fetch_telegram_updates(token: str, offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    response = telegram_api(token, "getUpdates", params)
    return list(response.get("result") or [])


def split_telegram_message(message: str) -> list[str]:
    if len(message) <= TELEGRAM_MESSAGE_LIMIT:
        return [message]

    chunks: list[str] = []
    remaining = message
    while len(remaining) > TELEGRAM_MESSAGE_LIMIT:
        split_at = remaining.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at < TELEGRAM_MESSAGE_LIMIT // 2:
            split_at = TELEGRAM_MESSAGE_LIMIT
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    for chunk in split_telegram_message(message):
        telegram_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            },
        )


def load_config(path: Path) -> ChatConfig:
    if not path.exists():
        return ChatConfig()

    data = json.loads(path.read_text(encoding="utf-8"))
    return ChatConfig(
        telegram_update_offset=data.get("telegram_update_offset"),
        enabled=bool(data.get("enabled", True)),
        model=normalize_model(str(data.get("model") or DEFAULT_MODEL)),
        system_instruction=str(data.get("system_instruction") or DEFAULT_SYSTEM_INSTRUCTION),
        google_cloud_project_id=str(
            data.get("google_cloud_project_id") or DEFAULT_GOOGLE_CLOUD_PROJECT_ID
        ),
        google_quota_project_id=(
            str(data.get("google_quota_project_id"))
            if data.get("google_quota_project_id")
            else DEFAULT_GOOGLE_QUOTA_PROJECT_ID
        ),
        daily_request_limit=clean_non_negative_int(
            data.get("daily_request_limit"),
            DEFAULT_DAILY_REQUEST_LIMIT,
        ),
        quota_date=data.get("quota_date"),
        quota_requests=clean_non_negative_int(data.get("quota_requests")),
        quota_prompt_tokens=clean_non_negative_int(data.get("quota_prompt_tokens")),
        quota_output_tokens=clean_non_negative_int(data.get("quota_output_tokens")),
        quota_total_tokens=clean_non_negative_int(data.get("quota_total_tokens")),
    )


def save_config(path: Path, config: ChatConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "daily_request_limit": config.daily_request_limit,
        "enabled": config.enabled,
        "google_cloud_project_id": config.google_cloud_project_id,
        "google_quota_project_id": config.google_quota_project_id,
        "model": config.model,
        "quota_date": config.quota_date,
        "quota_output_tokens": config.quota_output_tokens,
        "quota_prompt_tokens": config.quota_prompt_tokens,
        "quota_requests": config.quota_requests,
        "quota_total_tokens": config.quota_total_tokens,
        "system_instruction": config.system_instruction,
        "telegram_update_offset": config.telegram_update_offset,
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def current_quota_date(now: datetime | None = None) -> str:
    selected = now or datetime.now(QUOTA_TIMEZONE)
    return selected.astimezone(QUOTA_TIMEZONE).date().isoformat()


def reset_quota_if_needed(config: ChatConfig, now: datetime | None = None) -> None:
    today = current_quota_date(now)
    if config.quota_date == today:
        return

    config.quota_date = today
    config.quota_requests = 0
    config.quota_prompt_tokens = 0
    config.quota_output_tokens = 0
    config.quota_total_tokens = 0


def record_gemini_usage(config: ChatConfig, usage: GeminiUsage) -> None:
    reset_quota_if_needed(config)
    config.quota_requests += 1
    config.quota_prompt_tokens += usage.prompt_tokens
    config.quota_output_tokens += usage.output_tokens
    config.quota_total_tokens += usage.total_tokens


def format_count(value: int) -> str:
    return f"{value:,}"


def format_quota_value(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value >= UNLIMITED_QUOTA_VALUE:
        return "unlimited"
    return format_count(value)


def format_quota_pair(usage: int | None, limit: int | None) -> str:
    used = usage or 0
    if limit is None:
        return f"{format_count(used)} / unknown"
    if limit >= UNLIMITED_QUOTA_VALUE:
        return f"{format_count(used)} / unlimited"
    remaining = max(limit - used, 0)
    return f"{format_count(used)} / {format_count(limit)} ({format_count(remaining)} remaining)"


def load_service_account_credentials(raw_credentials: str) -> dict[str, Any]:
    credentials = json.loads(raw_credentials)
    required_fields = {"client_email", "private_key"}
    missing = sorted(field for field in required_fields if not credentials.get(field))
    if missing:
        raise ValueError(f"Service account JSON missing: {', '.join(missing)}")
    return credentials


def sign_service_account_jwt(credentials: dict[str, Any], scope: str) -> str:
    issued_at = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": credentials["client_email"],
        "scope": scope,
        "aud": credentials.get("token_uri") or GOOGLE_OAUTH_TOKEN_URL,
        "iat": issued_at,
        "exp": issued_at + 3600,
    }
    signing_input = ".".join(
        [
            base64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    ).encode("ascii")

    key_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as key_file:
            key_file.write(credentials["private_key"])
            key_path = key_file.name
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input,
            capture_output=True,
            check=True,
        )
    finally:
        if key_path:
            Path(key_path).unlink(missing_ok=True)

    return f"{signing_input.decode('ascii')}.{base64url(result.stdout)}"


def fetch_google_access_token(raw_credentials: str) -> str:
    credentials = load_service_account_credentials(raw_credentials)
    payload: dict[str, Any] = {}
    last_error: Exception | None = None
    for attempt in range(3):
        assertion = sign_service_account_jwt(
            credentials,
            "https://www.googleapis.com/auth/monitoring.read",
        )
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode("utf-8")
        try:
            payload = fetch_json(
                credentials.get("token_uri") or GOOGLE_OAUTH_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 400 or attempt == 2:
                raise
            time.sleep(5)
    if not payload and last_error is not None:
        raise last_error

    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Google OAuth token response did not include access_token.")
    return token


def point_value(point: dict[str, Any]) -> int | None:
    value = point.get("value") or {}
    for key in ("int64Value", "doubleValue"):
        if key in value:
            return clean_non_negative_int(value[key])
    return None


def query_monitoring_time_series(
    project_id: str,
    access_token: str,
    metric_type: str,
    start_time: datetime,
    end_time: datetime,
    quota_project_id: str | None = None,
) -> list[dict[str, Any]]:
    time_series: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params = {
            "filter": f'metric.type = "{metric_type}"',
            "interval.startTime": start_time.astimezone(ZoneInfo("UTC"))
            .isoformat()
            .replace("+00:00", "Z"),
            "interval.endTime": end_time.astimezone(ZoneInfo("UTC"))
            .isoformat()
            .replace("+00:00", "Z"),
            "view": "FULL",
            "pageSize": "1000",
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{MONITORING_TIME_SERIES_URL.format(project=project_id)}?{urllib.parse.urlencode(params)}"
        headers = {"Authorization": f"Bearer {access_token}"}
        if quota_project_id:
            headers["x-goog-user-project"] = quota_project_id
        payload = fetch_json(url, headers=headers)
        time_series.extend(payload.get("timeSeries") or [])
        page_token = str(payload.get("nextPageToken") or "")
        if not page_token:
            return time_series


def quota_stat_for_model(stats: dict[str, ProjectQuotaStat], model: str) -> ProjectQuotaStat:
    if model not in stats:
        stats[model] = ProjectQuotaStat(model=model)
    return stats[model]


def update_project_quota_stats(
    stats: dict[str, ProjectQuotaStat],
    series: list[dict[str, Any]],
    metric_name: str,
) -> None:
    is_limit_metric = metric_name.endswith("/limit")
    for item in series:
        labels = item.get("metric", {}).get("labels", {})
        model = labels.get("model")
        quota_kind = LIMIT_NAME_TO_QUOTA_KIND.get(str(labels.get("limit_name") or ""))
        if not model or not quota_kind:
            continue

        values = [point_value(point) for point in item.get("points") or []]
        numeric_values = [value for value in values if value is not None]
        if not numeric_values:
            continue

        selected_value = max(numeric_values)
        stat = quota_stat_for_model(stats, str(model))
        field_name = f"{quota_kind}_{'limit' if is_limit_metric else 'usage'}"
        setattr(stat, field_name, selected_value)


def fetch_project_quota_stats(
    project_id: str,
    raw_credentials: str,
    quota_project_id: str | None = None,
    days: int = PROJECT_QUOTA_DAYS,
) -> list[ProjectQuotaStat]:
    access_token = fetch_google_access_token(raw_credentials)
    end_time = datetime.now(ZoneInfo("UTC"))
    start_time = end_time - timedelta(days=days)
    stats: dict[str, ProjectQuotaStat] = {}
    for metric_name in PROJECT_QUOTA_METRICS.values():
        series = query_monitoring_time_series(
            project_id,
            access_token,
            metric_name,
            start_time,
            end_time,
            quota_project_id,
        )
        update_project_quota_stats(stats, series, metric_name)
    return sorted(stats.values(), key=lambda stat: stat.model)


def format_project_quota_message(
    config: ChatConfig,
    stats: list[ProjectQuotaStat],
    days: int = PROJECT_QUOTA_DAYS,
) -> str:
    if not stats:
        return (
            "Gemini project quota (free tier):\n"
            f"Project: {config.google_cloud_project_id}\n"
            f"No GenerateContent quota usage was found in the last {days} days."
        )

    lines = [
        "Gemini project quota (free tier, project-wide):",
        f"Project: {config.google_cloud_project_id}",
        f"Quota project: {config.google_quota_project_id or 'default'}",
        f"Peak usage over last {days} days:",
    ]
    for stat in stats:
        lines.extend(
            [
                "",
                stat.model,
                f"RPM: {format_quota_pair(stat.rpm_usage, stat.rpm_limit)}",
                f"TPM: {format_quota_pair(stat.tpm_usage, stat.tpm_limit)}",
                f"RPD: {format_quota_pair(stat.rpd_usage, stat.rpd_limit)}",
            ]
        )
    lines.append("")
    lines.append("These are Google Cloud Monitoring quota metrics for the whole project.")
    return "\n".join(lines)


def build_project_quota_message(
    config: ChatConfig,
    raw_credentials: str | None,
) -> str:
    if not raw_credentials:
        return (
            build_quota_message(config)
            + "\n\nProject-wide quota is unavailable because GOOGLE_SERVICE_ACCOUNT_JSON is not set."
        )
    stats = fetch_project_quota_stats(
        config.google_cloud_project_id,
        raw_credentials,
        config.google_quota_project_id,
    )
    return format_project_quota_message(config, stats)


def normalize_command(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    command = parts[0].split("@", maxsplit=1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return command, arg


def build_help_message(config: ChatConfig) -> str:
    state = "active" if config.enabled else "paused"
    return "\n".join(
        [
            f"Gemini chat bot is {state}.",
            f"Model: {config.model}",
            "",
            "Commands:",
            "/help",
            "/status",
            "/pause",
            "/resume",
            "/model gemini-2.0-flash",
            "/quota",
            "",
            "Send any normal text message to chat with Gemini.",
            "Messages starting with / are handled as commands and are never sent to Gemini.",
        ]
    )


def build_status_message(config: ChatConfig) -> str:
    state = "active" if config.enabled else "paused"
    offset = config.telegram_update_offset if config.telegram_update_offset is not None else "not set"
    return "\n".join(
        [
            f"Status: {state}",
            f"Model: {config.model}",
            f"Telegram update offset: {offset}",
            "Conversation history is not stored in the repository.",
        ]
    )


def build_quota_message(config: ChatConfig) -> str:
    reset_quota_if_needed(config)
    remaining = max(config.daily_request_limit - config.quota_requests, 0)
    return "\n".join(
        [
            "Gemini quota tracked by this bot:",
            f"Model: {config.model}",
            f"Date: {config.quota_date} Pacific",
            (
                "Requests today: "
                f"{format_count(config.quota_requests)} of "
                f"{format_count(config.daily_request_limit)} "
                f"({format_count(remaining)} remaining)"
            ),
            (
                "Tokens today: "
                f"{format_count(config.quota_total_tokens)} total "
                f"({format_count(config.quota_prompt_tokens)} input, "
                f"{format_count(config.quota_output_tokens)} output)"
            ),
            "This excludes Gemini usage outside this bot.",
        ]
    )


def update_config_from_command(
    config: ChatConfig,
    text: str,
    project_quota_builder: Callable[[], str] | None = None,
) -> tuple[str, str | None]:
    command, arg = normalize_command(text)

    if command in {"/start", "/help"}:
        return build_help_message(config), None

    if command == "/status":
        return build_status_message(config), None

    if command == "/quota":
        if project_quota_builder is None:
            return build_quota_message(config), None
        try:
            return project_quota_builder(), None
        except Exception as exc:
            return (
                build_quota_message(config)
                + f"\n\nProject-wide quota is unavailable right now: {exc}",
                None,
            )

    if command == "/pause":
        config.enabled = False
        return "Paused. Send /resume to chat again.", None

    if command == "/resume":
        config.enabled = True
        return "Resumed. Send me a message to chat with Gemini.", None

    if command == "/model":
        if not arg:
            return f"Current model: {config.model}\nUsage: /model gemini-2.0-flash", None
        try:
            config.model = normalize_model(arg)
        except ValueError as exc:
            return str(exc), None
        return f"Model set to {config.model}.", None

    if command == "/ask":
        return "Send normal text without a leading / to chat with Gemini.", None

    if command.startswith("/"):
        return "Unknown command. Send /help to see available commands.", None

    if not config.enabled:
        return "The bot is paused. Send /resume to chat again.", None

    return "", text.strip()


def build_gemini_request(config: ChatConfig, prompt: str) -> dict[str, Any]:
    return {
        "systemInstruction": {
            "parts": [{"text": config.system_instruction}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024,
        },
    }


def extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        reason = feedback.get("blockReason") or "no candidate returned"
        return f"Gemini did not return a response ({reason})."

    candidate = candidates[0]
    content = candidate.get("content") or {}
    parts = content.get("parts") or []
    text = "\n".join(str(part.get("text") or "").strip() for part in parts).strip()
    if text:
        return text

    finish_reason = candidate.get("finishReason") or "empty response"
    return f"Gemini returned no text ({finish_reason})."


def extract_gemini_usage(payload: dict[str, Any]) -> GeminiUsage:
    metadata = payload.get("usageMetadata") or {}
    return GeminiUsage(
        prompt_tokens=clean_non_negative_int(metadata.get("promptTokenCount")),
        output_tokens=clean_non_negative_int(metadata.get("candidatesTokenCount")),
        total_tokens=clean_non_negative_int(metadata.get("totalTokenCount")),
    )


def generate_gemini_response(
    api_key: str,
    config: ChatConfig,
    prompt: str,
    requester: Callable[[str, bytes, dict[str, str]], dict[str, Any]] | None = None,
) -> GeminiResponse:
    model = normalize_model(config.model)
    url = GEMINI_API_URL.format(model=urllib.parse.quote(model, safe="-_."))
    body = json.dumps(build_gemini_request(config, prompt)).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    request = requester or fetch_json
    payload = request(url, body, headers)
    return GeminiResponse(
        text=extract_gemini_text(payload),
        usage=extract_gemini_usage(payload),
    )


def generate_gemini_reply(
    api_key: str,
    config: ChatConfig,
    prompt: str,
    requester: Callable[[str, bytes, dict[str, str]], dict[str, Any]] | None = None,
) -> str:
    return generate_gemini_response(api_key, config, prompt, requester).text


def config_snapshot(config: ChatConfig) -> tuple[Any, ...]:
    return (
        config.telegram_update_offset,
        config.enabled,
        config.model,
        config.system_instruction,
        config.google_cloud_project_id,
        config.google_quota_project_id,
        config.daily_request_limit,
        config.quota_date,
        config.quota_requests,
        config.quota_prompt_tokens,
        config.quota_output_tokens,
        config.quota_total_tokens,
    )


def process_telegram_messages(
    token: str,
    chat_id: str,
    gemini_api_key: str,
    config: ChatConfig,
    dry_run: bool,
    google_service_account_json: str | None = None,
) -> bool:
    updates = fetch_telegram_updates(token, config.telegram_update_offset)
    changed = False

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = update_id + 1
            if config.telegram_update_offset != next_offset:
                config.telegram_update_offset = next_offset
                changed = True

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != str(chat_id):
            continue

        text = str(message.get("text") or "").strip()
        if not text:
            continue

        before = config_snapshot(config)
        reply, prompt = update_config_from_command(
            config,
            text,
            project_quota_builder=lambda: build_project_quota_message(
                config,
                google_service_account_json,
            ),
        )
        if config_snapshot(config) != before:
            changed = True

        if prompt:
            if dry_run:
                reply = f"DRY_RUN enabled. Gemini prompt would be:\n{prompt}"
            else:
                response = generate_gemini_response(gemini_api_key, config, prompt)
                record_gemini_usage(config, response.usage)
                changed = True
                reply = response.text

        if reply:
            if dry_run:
                print(f"Telegram reply would be:\n{reply}")
            else:
                send_telegram_message(token, chat_id, reply)

        print(f"Processed Telegram message: {normalize_command(text)[0] or 'chat'}")

    return changed


def main() -> int:
    dry_run = env("DRY_RUN", "false").lower() in {"1", "true", "yes"}
    token = env("GEMINI_TELEGRAM_BOT_TOKEN")
    chat_id = env("GEMINI_TELEGRAM_CHAT_ID")
    gemini_api_key = env("GEMINI_API_KEY")
    google_service_account_json = env("GOOGLE_SERVICE_ACCOUNT_JSON")
    config_path = Path(env("CONFIG_PATH", DEFAULT_CONFIG_PATH) or DEFAULT_CONFIG_PATH)

    if not token or not chat_id:
        raise SystemExit("GEMINI_TELEGRAM_BOT_TOKEN and GEMINI_TELEGRAM_CHAT_ID are required.")
    if not gemini_api_key and not dry_run:
        raise SystemExit("GEMINI_API_KEY is required.")

    config = load_config(config_path)
    if process_telegram_messages(
        token,
        chat_id,
        gemini_api_key or "",
        config,
        dry_run,
        google_service_account_json,
    ):
        save_config(config_path, config)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
