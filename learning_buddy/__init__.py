"""Learning Buddy — inbox/classify/NotebookLM agent."""

import os as _os

from dotenv import load_dotenv as _load_dotenv

# Load shared API keys (OPENAI_API_KEY, etc.) from ~/.env.
# book_chunker.py runs as a subprocess and loads ~/.env itself.
_load_dotenv(_os.path.expanduser("~/.env"))

from .agent import Agent, RunSummary  # noqa: E402
from .catalog import Catalog  # noqa: E402
from .config import AppConfig, load_config, save_config  # noqa: E402

__all__ = ["Agent", "AppConfig", "Catalog", "RunSummary", "load_config", "save_config"]
