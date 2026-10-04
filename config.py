"""Course project configuration.

In normal use, this is the main file to inspect or edit. API key, model, test file, and output paths are configured here.
"""

import getpass
import os
from pathlib import Path


# =============================================================================
# 1. OpenRouter API key
# =============================================================================

# Recommended: set the OPENROUTER_API_KEY environment variable before running.
# This avoids accidentally publishing a key to GitHub.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# For temporary local testing, you may paste the key into the string below.
# Clear this value before uploading the project to GitHub.
PASTE_API_KEY_HERE = ""


def get_api_key() -> str:
    """Get the API key; if it is not configured, ask securely at runtime."""
    key = OPENROUTER_API_KEY or PASTE_API_KEY_HERE.strip()
    if not key:
        key = getpass.getpass("Enter OpenRouter API Key (input will be hidden): ").strip()
    if not key:
        raise ValueError("No OpenRouter API Key was provided.")
    return key


# =============================================================================
# 2. Model and API endpoint
# =============================================================================

# Fixed model for reproducibility; the instructor only needs to provide an OpenRouter API key.
MODEL = "openai/gpt-4.1-mini"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT_SECONDS = 180


# =============================================================================
# 3. Input, output, and small batch size
# =============================================================================

PROJECT_DIR = Path(__file__).resolve().parent
TEST_FILE = PROJECT_DIR / "test_news.md"
RESULTS_DIR = PROJECT_DIR / "results"

# Each web-search request checks one item, so the search log, evidence, and result align clearly.
# Multiple one-item requests are still run concurrently by the main program.
BATCH_SIZE = 1
