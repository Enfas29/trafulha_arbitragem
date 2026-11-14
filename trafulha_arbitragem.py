import os
import re
import json
import time
import threading
import logging
import hashlib
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


# ============================================================
# 1. CONFIGURACOES (PAPER TRADING V8.2 - EXACT BUTTON SYNC)
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
SX_API_KEY = os.getenv("SX_API_KEY", "")
POLY_API_KEY = os.getenv("POLY_API_KEY", "")

PAPER_INITIAL_BANKROLL_POLY = float(os.getenv("PAPER_INITIAL_BANKROLL_POLY", "1000"))
PAPER_INITIAL_BANKROLL_SX = float(os.getenv("PAPER_INITIAL_BANKROLL_SX", "1000"))
PAPER_STAKE_USD = float(os.getenv("PAPER_STAKE_USD", "100"))

MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.5"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

MIN_POLY_VOLUME = float(os.getenv("MIN_POLY_VOLUME", "1000"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "15"))

POLY_FEE_BUFFER_PCT = float(os.getenv("POLY_FEE_BUFFER_PCT", "1.0"))
SX_FEE_BUFFER_PCT = float(os.getenv("SX_FEE_BUFFER_PCT", "0.0"))

MAX_POLY_PAGES = int(os.getenv("MAX_POLY_PAGES", "15"))
MAX_POLY_MARKETS = int(os.getenv("MAX_POLY_MARKETS", "400"))

AUTO_BET = False

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
SX_BASE = "https://api.sx.bet"


# ============================================================
# 2. LOGGING E ESTADO DA CARTEIRA DUPLA
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ArbitragePaperBotV83")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache"
})

if SX_API_KEY:
    session.headers.update({"x-sx-api-key": SX_API_KEY})

if POLY_API_KEY:
    session.headers.update({"POLY_API_KEY": POLY_API_KEY})

state_lock = threading.Lock()
wallet_lock = threading.Lock()

paper_wallet = {
    "poly": {
        "initial": PAPER_INITIAL_BANKROLL_POLY,
        "available": PAPER_INITIAL_BANKROLL_POLY,
        "invested": 0.0,
        "profit": 0.0
    },
    "sx": {
        "initial": PAPER_INITIAL_BANKROLL_SX,
        "available": PAPER_INITIAL_BANKROLL_SX,
        "invested": 0.0,
        "profit": 0.0
    },
    "bets_placed": 0,
    "history": [],
    "apostas_realizadas_hashes": set()
}

scanner_status = {
    "running": True,
    "last_scan": None,
    "last_error": None,
    "poly_games_checked": 0,
    "sx_markets_checked": 0,
