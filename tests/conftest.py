"""Pytest configuration: stub env vars so `import bot` succeeds without a real Discord/Anthropic/TN setup."""
import os
import sys
from pathlib import Path

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("CH_CLAUDE_INTEGRATION", "1")
os.environ.setdefault("CH_ASIA_SESSION", "2")
os.environ.setdefault("CH_LONDON_SESSION", "3")
os.environ.setdefault("CH_US_SESSION", "4")
os.environ.setdefault("CH_REGIME_OUTLOOK", "5")
os.environ.setdefault("CH_TRADES", "6")
os.environ.setdefault("TN_THREAD_CACHE", "/tmp/tn_thread_cache_test.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
