import unittest

from services import chatgpt_modules


class ChatGptModulesTests(unittest.TestCase):
    def test_parse_chatgpt_module_keys_accepts_json_array(self):
        parsed = chatgpt_modules.parse_chatgpt_module_keys('["cpa","sub2api","cpa","bad"]')
        self.assertEqual(parsed, ["cpa", "sub2api"])

    def test_get_enabled_chatgpt_modules_defaults_to_all(self):
        old_get_config_value = chatgpt_modules._get_config_value
        chatgpt_modules._get_config_value = lambda key, default="": ""
        try:
            enabled = chatgpt_modules.get_enabled_chatgpt_modules()
        finally:
            chatgpt_modules._get_config_value = old_get_config_value

        self.assertEqual(enabled, list(chatgpt_modules.CHATGPT_MODULE_KEYS))

    def test_is_chatgpt_module_enabled_uses_config_value(self):
        old_get_config_value = chatgpt_modules._get_config_value
        chatgpt_modules._get_config_value = lambda key, default="": '["cpa","smstome"]'
        try:
            self.assertTrue(chatgpt_modules.is_chatgpt_module_enabled("cpa"))
            self.assertFalse(chatgpt_modules.is_chatgpt_module_enabled("sub2api"))
        finally:
            chatgpt_modules._get_config_value = old_get_config_value


if __name__ == "__main__":
    unittest.main()
