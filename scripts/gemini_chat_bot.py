#!/usr/bin/env python3
"""Telegram-controlled Gemini chat bot for GitHub Actions polling."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


DEFAULT_CONFIG_PATH = "config/gemini_chat.json"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_DAILY_REQUEST_LIMIT = 250
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a concise Telegram assistant. Answer directly and ask a clarifying "
    "question only when needed."
)
QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"
MODEL_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
TELEGRAM_MESSAGE_LIMIT = 3900


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
class ChatConfig:
    telegram_update_offset: int | None = None
    enabled: bool = True
    model: str = DEFAULT_MODEL
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION
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
        raise ValueError("Use a Gemini model id like gemini-2.5-flash.")
    return model


def clean_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


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
            "/model gemini-2.5-flash",
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


def update_config_from_command(config: ChatConfig, text: str) -> tuple[str, str | None]:
    command, arg = normalize_command(text)

    if command in {"/start", "/help"}:
        return build_help_message(config), None

    if command == "/status":
        return build_status_message(config), None

    if command == "/quota":
        return build_quota_message(config), None

    if command == "/pause":
        config.enabled = False
        return "Paused. Send /resume to chat again.", None

    if command == "/resume":
        config.enabled = True
        return "Resumed. Send me a message to chat with Gemini.", None

    if command == "/model":
        if not arg:
            return f"Current model: {config.model}\nUsage: /model gemini-2.5-flash", None
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
        reply, prompt = update_config_from_command(config, text)
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
    config_path = Path(env("CONFIG_PATH", DEFAULT_CONFIG_PATH) or DEFAULT_CONFIG_PATH)

    if not token or not chat_id:
        raise SystemExit("GEMINI_TELEGRAM_BOT_TOKEN and GEMINI_TELEGRAM_CHAT_ID are required.")
    if not gemini_api_key and not dry_run:
        raise SystemExit("GEMINI_API_KEY is required.")

    config = load_config(config_path)
    if process_telegram_messages(token, chat_id, gemini_api_key or "", config, dry_run):
        save_config(config_path, config)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
