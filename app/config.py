import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"

# "claude_code": run agents through the local Claude Code CLI login (no API key needed).
# "api": call the Claude API with ANTHROPIC_API_KEY (pay-as-you-go).
LLM_BACKEND = os.getenv("LLM_BACKEND") or ("api" if os.getenv("ANTHROPIC_API_KEY") else "claude_code")

# API backend
MODEL = os.getenv("ICECREAM_MODEL", "claude-opus-5-5")
MAX_TOKENS = 16000

# Claude Code backend
CLAUDE_BIN = os.getenv("CLAUDE_BIN")  # auto-detected from PATH when unset
CLAUDE_CODE_MODEL = os.getenv("CLAUDE_CODE_MODEL", "sonnet")
CLAUDE_CODE_ROUTER_MODEL = os.getenv("CLAUDE_CODE_ROUTER_MODEL", "haiku")
CLAUDE_CODE_TIMEOUT = float(os.getenv("CLAUDE_CODE_TIMEOUT", "90"))
# Where the CLI reaches this app's /mcp tool endpoint; must match the uvicorn host/port.
APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000")

# Seconds spent in each status before advancing to the next one.
# In the POC one "simulated minute" is one real second, so ETAs are shown in minutes.
_speed = float(os.getenv("TRACKER_SPEED", "1.0"))
STATUS_DELAYS = {
    "PLACED": 6 * _speed,
    "PREPARING": 12 * _speed,
    "READY": 5 * _speed,
    "OUT_FOR_DELIVERY": 15 * _speed,
}

LOYALTY_DISCOUNT = {"none": 0, "silver": 5, "gold": 10}  # percent
MAX_COUPON_PERCENT = 25
