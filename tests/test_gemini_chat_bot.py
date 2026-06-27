import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import gemini_chat_bot


class GeminiChatBotTests(unittest.TestCase):
    def test_help_command_lists_controls(self):
        config = gemini_chat_bot.ChatConfig()

        reply, prompt = gemini_chat_bot.update_config_from_command(config, "/help")

        self.assertIsNone(prompt)
        self.assertIn("/pause", reply)
        self.assertIn("/model gemini-3.1-flash-lite", reply)
        self.assertIn("/quota", reply)
        self.assertIn("never sent to Gemini", reply)

    def test_pause_blocks_regular_chat(self):
        config = gemini_chat_bot.ChatConfig()

        reply, prompt = gemini_chat_bot.update_config_from_command(config, "/pause")
        blocked_reply, blocked_prompt = gemini_chat_bot.update_config_from_command(
            config,
            "hello",
        )

        self.assertEqual(reply, "Paused. Send /resume to chat again.")
        self.assertFalse(config.enabled)
        self.assertIsNone(blocked_prompt)
        self.assertIn("paused", blocked_reply)

    def test_model_command_updates_model(self):
        config = gemini_chat_bot.ChatConfig()

        reply, prompt = gemini_chat_bot.update_config_from_command(config, "/model models/gemini-2.0-flash")

        self.assertIsNone(prompt)
        self.assertEqual(config.model, "gemini-2.0-flash")
        self.assertEqual(reply, "Model set to gemini-2.0-flash.")

    def test_regular_text_becomes_prompt(self):
        config = gemini_chat_bot.ChatConfig()

        reply, prompt = gemini_chat_bot.update_config_from_command(config, "What is Python?")

        self.assertEqual(reply, "")
        self.assertEqual(prompt, "What is Python?")

    def test_slash_commands_are_never_sent_to_gemini(self):
        config = gemini_chat_bot.ChatConfig()

        reply, prompt = gemini_chat_bot.update_config_from_command(config, "/ask What is Python?")

        self.assertIsNone(prompt)
        self.assertIn("without a leading /", reply)

    def test_quota_command_reports_remaining_bot_tracked_usage(self):
        config = gemini_chat_bot.ChatConfig(
            daily_request_limit=250,
            quota_date=gemini_chat_bot.current_quota_date(),
            quota_requests=3,
            quota_prompt_tokens=100,
            quota_output_tokens=50,
            quota_total_tokens=150,
        )

        reply, prompt = gemini_chat_bot.update_config_from_command(config, "/quota")

        self.assertIsNone(prompt)
        self.assertIn("3 of 250", reply)
        self.assertIn("247 remaining", reply)
        self.assertIn("150 total", reply)

    def test_quota_command_uses_project_quota_builder_when_available(self):
        config = gemini_chat_bot.ChatConfig()

        reply, prompt = gemini_chat_bot.update_config_from_command(
            config,
            "/quota",
            project_quota_builder=lambda: "project quota",
        )

        self.assertIsNone(prompt)
        self.assertEqual(reply, "project quota")

    def test_format_project_quota_message_includes_model_limits(self):
        config = gemini_chat_bot.ChatConfig(
            google_cloud_project_id="project-id",
            google_quota_project_id="quota-project",
        )
        stats = [
            gemini_chat_bot.ProjectQuotaStat(
                model="gemini-2.5-flash",
                rpm_usage=1,
                rpm_limit=5,
                tpm_usage=30,
                tpm_limit=250000,
                rpd_usage=3,
                rpd_limit=20,
            )
        ]

        reply = gemini_chat_bot.format_project_quota_message(config, stats)

        self.assertIn("project-wide", reply)
        self.assertIn("Project: project-id", reply)
        self.assertIn("Quota project: quota-project", reply)
        self.assertIn("gemini-2.5-flash", reply)
        self.assertIn("RPM: 1 / 5 (4 remaining)", reply)
        self.assertIn("TPM: 30 / 250,000 (249,970 remaining)", reply)
        self.assertIn("RPD: 3 / 20 (17 remaining)", reply)

    def test_generate_gemini_reply_sends_expected_request(self):
        config = gemini_chat_bot.ChatConfig(model="gemini-3.1-flash-lite")
        captured = {}

        def requester(url, data, headers):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = data.decode("utf-8")
            return {
                "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 5,
                },
            }

        reply = gemini_chat_bot.generate_gemini_reply("secret", config, "hello", requester)

        self.assertEqual(reply, "answer")
        self.assertIn("/models/gemini-3.1-flash-lite:generateContent", captured["url"])
        self.assertEqual(captured["headers"]["x-goog-api-key"], "secret")
        self.assertIn("hello", captured["body"])

    def test_record_gemini_usage_updates_daily_quota_counters(self):
        config = gemini_chat_bot.ChatConfig(quota_date=gemini_chat_bot.current_quota_date())

        gemini_chat_bot.record_gemini_usage(
            config,
            gemini_chat_bot.GeminiUsage(prompt_tokens=2, output_tokens=3, total_tokens=5),
        )

        self.assertEqual(config.quota_requests, 1)
        self.assertEqual(config.quota_prompt_tokens, 2)
        self.assertEqual(config.quota_output_tokens, 3)
        self.assertEqual(config.quota_total_tokens, 5)

    def test_process_updates_advances_offset_for_ignored_chats(self):
        config = gemini_chat_bot.ChatConfig()
        updates = [
            {"update_id": 10, "message": {"chat": {"id": "other"}, "text": "ignore"}},
            {"update_id": 11, "message": {"chat": {"id": "chat"}, "text": "/status"}},
        ]

        with mock.patch.object(gemini_chat_bot, "fetch_telegram_updates", return_value=updates):
            with mock.patch.object(gemini_chat_bot, "send_telegram_message") as send:
                changed = gemini_chat_bot.process_telegram_messages(
                    "token",
                    "chat",
                    "gemini-key",
                    config,
                    dry_run=False,
                )

        self.assertTrue(changed)
        self.assertEqual(config.telegram_update_offset, 12)
        send.assert_called_once()

    def test_load_and_save_config_round_trip(self):
        config = gemini_chat_bot.ChatConfig(
            telegram_update_offset=123,
            enabled=False,
            model="gemini-3.1-flash-lite",
            system_instruction="Be brief.",
            google_cloud_project_id="project-id",
            google_quota_project_id="quota-project",
            daily_request_limit=100,
            quota_date="2026-06-26",
            quota_requests=5,
            quota_prompt_tokens=10,
            quota_output_tokens=20,
            quota_total_tokens=30,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gemini_chat.json"
            gemini_chat_bot.save_config(path, config)
            loaded = gemini_chat_bot.load_config(path)

        self.assertEqual(loaded, config)


if __name__ == "__main__":
    unittest.main()
