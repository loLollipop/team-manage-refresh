import unittest

from app.services.team import TeamService


class TeamImportNormalizationTests(unittest.TestCase):
    def test_cli_proxy_api_flat_auth_file(self):
        item = {
            "type": "codex",
            "access_token": " at-flat ",
            "refresh_token": "rt-flat",
            "id_token": "id-flat",
            "account_id": "account-flat",
            "email": "flat@example.com",
            "expired": "2026-08-12T12:00:00+08:00",
        }

        self.assertEqual(
            TeamService._normalize_team_import_item(item),
            {
                "access_token": "at-flat",
                "id_token": "id-flat",
                "refresh_token": "rt-flat",
                "session_token": None,
                "client_id": None,
                "email": "flat@example.com",
                "account_id": "account-flat",
            },
        )

    def test_nested_token_object_and_metadata_are_supported(self):
        item = {
            "type": "codex",
            "metadata": {
                "email": "nested@example.com",
                "token": {
                    "access_token": "at-nested",
                    "refresh_token": "rt-nested",
                    "id_token": "id-nested",
                    "chatgpt_account_id": "account-nested",
                },
            },
        }

        normalized = TeamService._normalize_team_import_item(item)
        self.assertEqual(normalized["access_token"], "at-nested")
        self.assertEqual(normalized["refresh_token"], "rt-nested")
        self.assertEqual(normalized["id_token"], "id-nested")
        self.assertEqual(normalized["email"], "nested@example.com")
        self.assertEqual(normalized["account_id"], "account-nested")

    def test_legacy_string_token_is_treated_as_access_token(self):
        normalized = TeamService._normalize_team_import_item(
            {"token": "at-legacy", "email": "legacy@example.com"}
        )

        self.assertEqual(normalized["access_token"], "at-legacy")
        self.assertEqual(normalized["email"], "legacy@example.com")

    def test_unrelated_json_object_is_ignored(self):
        self.assertIsNone(TeamService._normalize_team_import_item({"type": "codex"}))


if __name__ == "__main__":
    unittest.main()
