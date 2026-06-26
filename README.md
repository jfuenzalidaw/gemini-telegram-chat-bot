# Gemini Telegram Chat Bot

Telegram-controlled Gemini chat bot that runs through GitHub Actions.

The bot polls Telegram every 30 seconds while each GitHub Actions run is active. GitHub's shortest supported cron interval is five minutes, so the workflow runs 10 polling cycles per scheduled run and can self-dispatch the next run when configured.

## Files

```text
scripts/gemini_chat_bot.py
config/gemini_chat.json
.github/workflows/gemini-telegram-chat.yml
```

## Setup

1. Create or rotate the Telegram bot token for `@Gemini_jfw_bot` in BotFather.
2. Create or rotate the Gemini API key in Google AI Studio.
3. Send `/start` to `t.me/Gemini_jfw_bot`.
4. Find your Telegram chat id by opening this URL with your bot token:

   ```text
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```

5. Add these GitHub Actions Secrets:

   ```text
   GEMINI_TELEGRAM_BOT_TOKEN
   GEMINI_TELEGRAM_CHAT_ID
   GEMINI_API_KEY
   GEMINI_GH_WORKFLOW_PAT
   ```

Do not commit raw tokens, chat IDs, API keys, or passwords. If a key or token was shared in chat or screenshots, rotate it before using this bot long-term.

## Telegram Commands

```text
/help
/status
/pause
/resume
/model gemini-2.5-flash
/quota
```

Normal text messages are sent to Gemini and answered in Telegram.
Messages starting with `/` are handled as commands and are never sent to Gemini.

Conversation history is not stored in the repository. The config file stores only the Telegram update offset, enabled state, selected model, default system instruction, and bot-tracked daily quota counters.

`/quota` reports requests and tokens used by this bot today, compared with the configured daily request limit in `config/gemini_chat.json`. Gemini quotas are enforced per Google Cloud project, so this command does not include usage from AI Studio, other apps, or other API keys in the same project.

## Local Test

Run the unit tests:

```bash
python3 -m unittest discover -v
```

Run a dry poll without calling Gemini or sending Telegram replies:

```bash
GEMINI_TELEGRAM_BOT_TOKEN="your-token" \
GEMINI_TELEGRAM_CHAT_ID="your-chat-id" \
DRY_RUN=true \
python3 scripts/gemini_chat_bot.py
```
