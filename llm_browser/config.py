"""Configuration and path management."""

from pathlib import Path

# All persistent data lives under ~/.config/llm-browser/
CONFIG_DIR = Path.home() / ".config" / "llm-browser"
PROFILE_DIR = CONFIG_DIR / "browser-profile"   # persistent Chromium profile
LOGS_DIR = CONFIG_DIR / "logs"
DB_PATH = CONFIG_DIR / "history.db"

# Ensure dirs exist at import time (safe to call multiple times)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Timeouts (milliseconds)
NAVIGATION_TIMEOUT = 30_000
RESPONSE_TIMEOUT = 120_000      # 2 min max for a full LLM response
POLL_INTERVAL_MS = 500          # DOM polling cadence
STREAM_SETTLE_MS = 2_000        # wait after last token before declaring done

# Provider identifiers
PROVIDER_ALIASES: dict[str, str] = {
    "claude": "claude",
    "c": "claude",
    "chatgpt": "chatgpt",
    "gpt": "chatgpt",
    "openai": "chatgpt",
    "gemini": "gemini",
    "g": "gemini",
    "bard": "gemini",
}