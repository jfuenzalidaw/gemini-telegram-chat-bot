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
        self.assertIn("/model gemini-2.5-flash", reply)

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

    def test_generate_gemini_reply_sends_expected_request(self):
        config = gemini_chat_bot.ChatConfig(model="gemini-2.5-flash")
        captured = {}

        def requester(url, data, headers):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = data.decode("utf-8")
            return {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}

        reply = gemini_chat_bot.generate_gemini_reply("secret", config, "hello", requester)

        self.assertEqual(reply, "answer")
        self.assertIn("/models/gemini-2.5-flash:generateContent", captured["url"])
        self.assertEqual(captured["headers"]["x-goog-api-key"], "secret")
        self.assertIn("hello", captured["body"])

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
            model="gemini-2.0-flash",
            system_instruction="Be brief.",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gemini_chat.json"
            gemini_chat_bot.save_config(path, config)
            loaded = gemini_chat_bot.load_config(path)

        self.assertEqual(loaded, config)


if __name__ == "__main__":
    unittest.main()
