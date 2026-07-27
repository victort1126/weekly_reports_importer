"""Central configuration loaded from environment variables (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Config:
    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_channel_id: str | None

    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_deployment: str
    azure_openai_api_version: str

    # Google Sheets
    google_oauth_client_path: str
    google_token_path: str
    google_spreadsheet_id: str
    roster_sheet_name: str
    report_log_sheet_name: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            slack_bot_token=_require("SLACK_BOT_TOKEN"),
            slack_app_token=_require("SLACK_APP_TOKEN"),
            slack_channel_id=os.environ.get("SLACK_CHANNEL_ID", "").strip() or None,
            azure_openai_endpoint=_require("AZURE_OPENAI_ENDPOINT"),
            azure_openai_api_key=_require("AZURE_OPENAI_API_KEY"),
            azure_openai_deployment=_require("AZURE_OPENAI_DEPLOYMENT"),
            azure_openai_api_version=os.environ.get(
                "AZURE_OPENAI_API_VERSION", "2025-04-01-preview"
            ).strip(),
            google_oauth_client_path=os.environ.get(
                "GOOGLE_OAUTH_CLIENT_PATH", "credentials/oauth_client.json"
            ).strip(),
            google_token_path=os.environ.get(
                "GOOGLE_TOKEN_PATH", "credentials/token.json"
            ).strip(),
            google_spreadsheet_id=_require("GOOGLE_SPREADSHEET_ID"),
            roster_sheet_name=os.environ.get("ROSTER_SHEET_NAME", "Roster").strip(),
            report_log_sheet_name=os.environ.get(
                "REPORT_LOG_SHEET_NAME", "Report Log"
            ).strip(),
        )


config = Config.from_env()
