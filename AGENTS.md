# Project Instructions

Build and maintain a Telegram-controlled Gemini chat bot that runs through GitHub Actions.

Security rules:
- Never commit raw tokens, chat IDs, GitHub PATs, API keys, or passwords.
- Store secrets only in GitHub Actions Secrets.
- Use placeholders in docs and prompts.
- If a token was pasted into chat or shown in a screenshot, rotate it in the provider UI before long-term use.

Architecture preferences:
- Python scripts, standard library first.
- No external dependencies unless clearly needed.
- Telegram commands for runtime control.
- GitHub Actions for scheduled, manual, and self-dispatched execution.
- Repository files for durable non-secret configuration.
- Tests should cover command parsing, config persistence, and Gemini request behavior.
- Use `python3 -m unittest discover -v` for validation.
- If faster than 5-minute cron is needed, use internal polling or self-dispatch, not unsupported cron syntax.

