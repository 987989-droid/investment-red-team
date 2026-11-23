import tempfile
import unittest
from pathlib import Path

from cli.privacy_check import (
    check_path_rules,
    is_valid_tw_national_id,
    main,
    scan_file,
    scan_text,
)


class PrivacyCheckTests(unittest.TestCase):
    def test_valid_tw_national_id_checksum(self):
        # Construct synthetic valid checksum ID dynamically (no literal PII in source)
        synthetic_id = "A" + "1" + "2345678" + "9"
        self.assertTrue(is_valid_tw_national_id(synthetic_id))
        self.assertFalse(is_valid_tw_national_id("A123456780"))

    def test_detects_forbidden_private_paths(self):
        self.assertTrue(check_path_rules("my-cases/2330-case.md"))
        self.assertTrue(check_path_rules("portfolio/holdings.yaml"))
        self.assertTrue(check_path_rules(".env"))
        self.assertTrue(check_path_rules("broker.statement.csv"))
        self.assertEqual(check_path_rules("templates/tw-stock.md"), [])

    def test_detects_sensitive_content_patterns(self):
        # Dynamically assembled synthetic strings to avoid storing literal PII in test source
        sample = "\n".join(
            [
                "聯絡電話: " + "0912" + "-" + "345" + "-" + "678",
                "信箱: " + "investor" + "@private-mail.tw",
                "券商帳號: " + "1234567890",
                "總資產: " + "NT" + "$ 1,250," + "000",
                "token: " + "ghp_" + "a" * 24,
            ]
        )
        findings = scan_text("notes.md", sample)
        rules = {f.rule for f in findings}
        self.assertIn("tw-mobile-phone", rules)
        self.assertIn("email-address", rules)
        self.assertIn("labeled-account-number", rules)
        self.assertIn("personal-holdings-amount", rules)
        self.assertIn("large-currency-amount", rules)
        self.assertIn("secret-token", rules)

    def test_allows_safe_emails_and_inline_ignore(self):
        safe = "\n".join(
            [
                "contact: demo@example.com",
                "bot: 12345+user@users.noreply.github.com",
                "總資產: 9999999 # privacy-check: ignore",
            ]
        )
        self.assertEqual(scan_text("README.md", safe), [])

    def test_cli_exit_code_on_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_file = root / "clean.md"
            clean_file.write_text("# Clean Framework Doc\nNo personal info.\n", encoding="utf-8")
            self.assertEqual(main(["--root", str(root), "--all"]), 0)

            leaked_file = root / "leak.md"
            leaked_file.write_text("持有張數: " + "42\n", encoding="utf-8")
            self.assertEqual(len(scan_file(leaked_file, root)), 1)
            self.assertEqual(main(["--root", str(root), "--all"]), 1)


if __name__ == "__main__":
    unittest.main()
