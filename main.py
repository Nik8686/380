# ============================================================
# PUMP.FUN HOLDERS + FEES HUNTER  +  AUTO BUY / AUTO SELL
# الفلاتر:  Holders > MIN_HOLDERS   و   Fees بين MIN_FEES_SOL و MAX_FEES_SOL
# أي عملة تدوز الفلاتر: شراء فوري (0.007 SOL) + TP +100% + SL -50%
# كيتبع العملة حتى بعد التخرج (migration) لـ PumpSwap وكيبيع من الـ pool الصحيح.
# ============================================================

import asyncio
import base64
import collections
import hashlib
import html
import json
import os
import struct
import time
from urllib.parse import urlparse

import aiohttp
import nest_asyncio
import websockets
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

# توافق مع Google Colab أو بيئة خارجية مثل Railway
try:
    from google.colab import userdata
except ImportError:
    class DummyUserdata:
        def get(self, name):
            return os.getenv(name)
    userdata = DummyUserdata()

nest_asyncio.apply()

# ============================================================
# SECRETS
# ============================================================

def _secret(name):
    try:
        return userdata.get(name)
    except Exception:
        return None

TG_TOKEN = _secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = _secret("TELEGRAM_CHAT_ID")
HELIUS_KEY = (_secret("HELIUS_API_KEY") or "").strip() or None
WALLET_SECRET = (_secret("TRADING_WALLET_PRIVATE_KEY") or "").strip() or None
print("Telegram Bot Token:", "OK ✅" if TG_TOKEN else "MISSING ❌")
print("Telegram Chat ID:", "OK ✅" if TG_CHAT else "MISSING ❌")
print("Helius key:", "OK ✅ (primary + holder verification)" if HELIUS_KEY else "not set (public nodes only)")
print("Trading wallet key:", "OK ✅" if WALLET_SECRET else "MISSING ❌")

def scrub(text):
    text = str(text)
    for secret in (TG_TOKEN, HELIUS_KEY, WALLET_SECRET):
        if secret:
            text = text.replace(secret, "***")
    return text

def load_keypair(secret):
    """كيقبل المفتاح base58 (Phantom) أو JSON array."""
    if not secret:
        return None
    try:
        if secret.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(secret)))
        return Keypair.from_base58_string(secret)
    except Exception as e:
        print(f"❌ Invalid private key format: {type(e).__name__}")
        return None

KEYPAIR = load_keypair(WALLET_SECRET)
OUR_PUB = str(KEYPAIR.pubkey()) if KEYPAIR else None
if OUR_PUB:
    print(f"Trading wallet: {OUR_PUB}")

# ============================================================
# SETTINGS (دون تغيير أي شيء من الفلاتر)
# ============================================================

MIN_HOLDERS = 380            # الهولدرز: أكثر من 350
MIN_FEES_SOL = 3.2           # الرسوم المتولدة من الصفقات: من 3.5 ...
MAX_FEES_SOL = 6.2           # ... حتى 8 SOL

FEE_RATE_FALLBACK = 0.0125   # إلا ما قدرناش نقراو الرسوم من الـ event: نقدرو حجم × 1.25%
VERIFY_HOLDERS = True        # إلا كاين Helius: تأكد من عدد الهولدرز الحقيقي قبل التنبيه

# ---------------- AUTO TRADE (كلشي تقدر تبدلو هنا) ----------------
AUTO_TRADE_ENABLED = False      # False = تنبيهات فقط بلا شراء
DRY_RUN = False                # True = محاكاة بلا معاملات حقيقية (جرب بيها أول مرة)
BUY_AMOUNT_SOL = 0.01         # حجم الصفقة
TAKE_PROFIT_PCT = 90.0        # جني الأرباح: +100% (x2)
STOP_LOSS_PCT = 50.0           # وقف الخسارة الأولي: -50% من سعر الدخول
TRAILING_STOP_PCT = 50.0       # Trailing stop: كيبيع إلا نزل السعر 50% من أعلى سعر وصلتو العملة (0 = معطل)
                               # باش تخلي غير الـ trailing بلا TP ثابت: دير TAKE_PROFIT_PCT = 0
MAX_OPEN_POSITIONS = 30         # أقصى عدد صفقات مفتوحة فنفس الوقت
MAX_BUYS_PER_HOUR = 100         # حماية من الشراء الزايد
VERIFY_BEFORE_BUY = False      # False = شراء فوري ملي تدوز الفلاتر (التأكد من الهولدرز كيتدار غير للتنبيه)
ALLOW_BUY_AFTER_GRAD = True    # شراء حتى إلا العملة تخرجات فنفس لحظة الفلتر (من PumpSwap)

BUY_SLIPPAGE_PCT = 15
BUY_PRIORITY_FEE_SOL = 0.0005
BUY_CONFIRM_SECONDS = 25
SELL_SLIPPAGE_PCT = 40         # slippage كبير للبيع باش ما يفشلش ملي السعر كيتحرك
SELL_PRIORITY_FEE_SOL = 0.001
SELL_CONFIRM_SECONDS = 8
SELL_RETRIES = 6
MAX_SELL_ROUNDS = 8
SELL_ROUND_COOLDOWN_SECONDS = 6

PRICE_STALE_S = 4              # إلا ما جاتش تريدات للعملة 4 ثواني: كنسولو RPC/Pool على السعر
WATCHDOG_INTERVAL_S = 1.5

PUMPPORTAL_TRADE_LOCAL = "https://pumpportal.fun/api/trade-local"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# --- تنظيف الذاكرة ---
INACTIVE_S = 20 * 60         # عملة ما تحركات 20 دقيقة = كنوقفو نتبعوها
MAX_TRACK_S = 6 * 3600
TOKENS_MAX = 6000
MAX_ALERTS_PER_HOUR = 40

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSS_NODES = []
if HELIUS_KEY:
    WSS_NODES.append(f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}")
WSS_NODES += ["wss://api.mainnet-beta.solana.com", "wss://solana-rpc.publicnode.com"]
HELIUS_HTTP = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}" if HELIUS_KEY else None
HTTP_RPCS = ([HELIUS_HTTP] if HELIUS_HTTP else []) + ["https://api.mainnet-beta.solana.com",
                                                     "https://solana-rpc.publicnode.com"]

SUPPLY = 1_000_000_000
INITIAL_VSOL = 30.0
INITIAL_VTOKENS = 1_073_000_000
DUST_TOKENS = 1.0            # أقل من توكن واحد ما كيتحسبش هولدر (باش نتفاداو بقايا float)

# ============================================================
# STATE
# ============================================================

SOL_USD = 0.0
WS = None
STATE = {}
toks = {}
COUNTERS = collections.Counter()
DISCS = collections.Counter()
SAMPLES = {"decode_fail": 0}
LAST_MSG = {"t": time.time()}
SEEN_SIGS = set()
SEEN_ORDER = collections.deque()
ALERT_TIMES = collections.deque()
positions = {}                              # mint -> position dict
BOUGHT = set()                              # عمر ما نشريو نفس العملة جوج مرات
BUY_TIMES = collections.deque()
EXITED = collections.deque(maxlen=200)     # (symbol, holders, fees) عملات خرجات من نافذة الرسوم

def sol_usd():
    return SOL_USD if SOL_USD > 0 else 150.0

def fmt_usd(x):
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:.0f}"

# ============================================================
# SOL PRICE (free)
# ============================================================

async def fetch_sol_price():
    sources = [
        ("https://api.coinbase.com/v2/prices/SOL-USD/spot", lambda d: float(d["data"]["amount"])),
        ("https://api.kraken.com/0/public/Ticker?pair=SOLUSD",
         lambda d: float(list(d["result"].values())[0]["c"][0])),
        ("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
         lambda d: float(d["solana"]["usd"])),
    ]
    async with aiohttp.ClientSession() as s:
        for url, parse in sources:
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        p = parse(await r.json())
                        if p > 0:
                            return p
            except Exception:
                continue
    return None

async def sol_price_loop():
    global SOL_USD
    while True:
        p = await fetch_sol_price()
        if p:
            SOL_USD = p
        await asyncio.sleep(60)

# ============================================================
# TELEGRAM
# ============================================================

async def tg(text, mint=None):
    if not TG_TOKEN or not TG_CHAT:
        print("❌ Telegram token/chat missing")
        return False
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if mint:
        payload["reply_markup"] = {"inline_keyboard": [
            [{"text": "🚀 Pump.fun", "url": f"https://pump.fun/coin/{mint}"},
             {"text": "📊 DexScreener", "url": f"https://dexscreener.com/solana/{mint}"}],
            [{"text": "🛡 RugCheck", "url": f"https://rugcheck.xyz/tokens/{mint}"},
             {"text": "🫧 Bubblemaps", "url": f"https://app.bubblemaps.io/sol/token/{mint}"}],
        ]}
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for _ in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        return True
                    body = await r.text()
                    if r.status == 429:
                        try:
                            wait = json.loads(body).get("parameters", {}).get("retry_after", 3)
                        except Exception:
                            wait = 3
                        await asyncio.sleep(min(wait, 30))
                        continue
                    print(f"❌ Telegram {r.status}: {scrub(body)[:200]}")
                    return False
        except Exception as e:
            print(f"❌ Telegram error: {scrub(e)[:200]}")
            await asyncio.sleep(1)
    return False

# ============================================================
# EVENT DECODING (Anchor events فـ "Program data:" logs)
# ============================================================

COMPLETE_DISC = hashlib.sha256(b"event:CompleteEvent").digest()[:8]
TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]     # bddb7fd34ee661ee
CREATE_DISC = hashlib.sha256(b"event:CreateEvent").digest()[:8]   # 1b72a94ddeeb6376
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _ALPHABET[r] + out
    return "1" * sum(1 for b in data if b == 0) + out

def decode_trade(raw):
    """الحقول الـ 10 الأولى ثابتة. الحقول الإضافية (الرسوم/creator) كنقراوها غير إلا بانت منطقية."""
    try:
        d = raw[8:]
        if len(d) < 121:
            return None
        o = 0
        mint = b58(d[o:o + 32]); o += 32
        sol = struct.unpack_from("<Q", d, o)[0]; o += 8
        tk = struct.unpack_from("<Q", d, o)[0]; o += 8
        is_buy = bool(d[o]); o += 1
        user = b58(d[o:o + 32]); o += 32
        ts = struct.unpack_from("<q", d, o)[0]; o += 8
        vs = struct.unpack_from("<Q", d, o)[0]; o += 8
        vt = struct.unpack_from("<Q", d, o)[0]; o += 8
        real_sol = struct.unpack_from("<Q", d, o)[0]; o += 8
        real_tok = struct.unpack_from("<Q", d, o)[0]
        if vt <= 0:
            return None
        vs_sol, vt_ui = vs / 1e9, vt / 1e6
        msg = {
            "txType": "buy" if is_buy else "sell", "mint": mint, "traderPublicKey": user,
            "solAmount": sol / 1e9, "tokenAmount": tk / 1e6, "timestamp": ts,
            "vSolInBondingCurve": vs_sol, "vTokensInBondingCurve": vt_ui,
            "marketCapSol": vs_sol * 1e9 / vt_ui,
            "price": vs_sol / vt_ui,
            "realSol": real_sol / 1e9, "realTok": real_tok / 1e6,
        }
        if len(d) >= 217 and sol > 0:
            try:
                p = 121 + 32
                fee_bps = struct.unpack_from("<Q", d, p)[0]
                fee = struct.unpack_from("<Q", d, p + 8)[0]
                creator = b58(d[p + 16:p + 48])
                cfee_bps = struct.unpack_from("<Q", d, p + 48)[0]
                cfee = struct.unpack_from("<Q", d, p + 56)[0]
                ratio = (fee + cfee) / sol
                if 0 < fee_bps <= 1000 and cfee_bps <= 1000 and 0.0005 <= ratio <= 0.05:
                    msg["fee_sol"] = (fee + cfee) / 1e9
                    if creator.strip("1"):
                        msg["creator"] = creator
            except Exception:
                pass
        return msg
    except Exception:
        return None

def decode_create(raw, strict=False):
    """CreateEvent: name, symbol, uri (borsh strings) ثم mint, bonding_curve, user."""
    try:
        d = raw[8:]
        o = 0

        def rd_str(o):
            n = struct.unpack_from("<I", d, o)[0]
            if n > 400:
                raise ValueError("string too long")
            return d[o + 4:o + 4 + n].decode("utf-8", "replace"), o + 4 + n

        name, o = rd_str(o)
        symbol, o = rd_str(o)
        uri, o = rd_str(o)
        if len(d) < o + 96:
            return None
        if strict and not uri.startswith(("http", "ipfs", "ar:")):
            return None
        mint = b58(d[o:o + 32]); o += 32
        o += 32
        user = b58(d[o:o + 32])
        return {"txType": "create", "mint": mint, "name": name, "symbol": symbol,
                "traderPublicKey": user}
    except Exception:
        return None

def process_notification(value):
    if value.get("err"):
        COUNTERS["failed_tx_skipped"] += 1
        return
    sig = value.get("signature")
    if not sig or sig in SEEN_SIGS:
        return
    SEEN_SIGS.add(sig)
    SEEN_ORDER.append(sig)
    if len(SEEN_ORDER) > 60_000:
        SEEN_SIGS.discard(SEEN_ORDER.popleft())

    logs = value.get("logs") or []
    is_create_tx = any("Instruction: Create" in l for l in logs)
    for log in logs:
        if "Program data:" not in log:
            continue
        try:
            raw = base64.b64decode(log.split("Program data:", 1)[1].strip())
        except Exception:
            continue
        disc = raw[:8]
        if disc == COMPLETE_DISC:
            if len(raw) >= 8 + 64:
                mint = b58(raw[8 + 32:8 + 64])
                if mint in positions:
                    mark_migrated(mint, "CompleteEvent")
            continue
        if disc == TRADE_DISC:
            msg = decode_trade(raw)
        elif disc == CREATE_DISC:
            COUNTERS["create_events"] += 1
            msg = decode_create(raw)
        elif is_create_tx:
            msg = decode_create(raw, strict=True)
            if msg:
                COUNTERS["create_alt"] += 1
            else:
                DISCS[disc.hex()] += 1
                continue
        else:
            DISCS[disc.hex()] += 1
            continue
        if msg is None:
            COUNTERS["decode_fail"] += 1
            if SAMPLES["decode_fail"] < 3:
                SAMPLES["decode_fail"] += 1
                print(f"🔎 decode failed (disc={disc.hex()}, len={len(raw)}): {raw[:80].hex()}")
            continue
        handle_msg(msg)

# ============================================================
# TOKEN STATE
# ============================================================

class Tok:
    def __init__(self, mint, now):
        self.mint = mint
        self.t0 = now
        self.last = now
        self.name = ""
        self.symbol = ""
        self.creator = None
        self.bal = {}
        self.holders = 0
        self.trades = 0
        self.buys = 0
        self.sells = 0
        self.buyers = set()
        self.buy_sol = 0.0
        self.sell_sol = 0.0
        self.fees = 0.0
        self.fees_est = 0.0
        self.mcap = 0.0
        self.max_holders = 0
        self.dev_sold = False
        self.state = "watching"
        self.retry_at = 0.0
        self.verified = None
        self.price = 0.0
        self.complete = False

def new_tok(mint, now):
    t = Tok(mint, now)
    toks[mint] = t
    COUNTERS["tracked_total"] += 1
    return t

def set_balance(t, w, new):
    new = max(0.0, new)
    old = t.bal.get(w, 0.0)
    if (old > DUST_TOKENS) != (new > DUST_TOKENS):
        t.holders += 1 if new > DUST_TOKENS else -1
    t.bal[w] = new
    if t.holders > t.max_holders:
        t.max_holders = t.holders

def on_create(msg, now=None):
    now = time.time() if now is None else now
    mint = msg.get("mint")
    if not mint:
        return None
    t = toks.get(mint) or new_tok(mint, now)
    t.name = t.name or str(msg.get("name") or "")[:40]
    t.symbol = t.symbol or str(msg.get("symbol") or "")[:20]
    if not t.creator and msg.get("traderPublicKey"):
        t.creator = msg["traderPublicKey"]
    return t

def on_trade(msg, now=None):
    now = time.time() if now is None else now
    mint = msg.get("mint")
    is_buy = msg.get("txType") == "buy"
    sol = float(msg.get("solAmount") or 0)
    COUNTERS["trade_msgs"] += 1
    if mint in positions:
        position_on_trade(msg, now)

    t = toks.get(mint)
    if t is None:
        if is_buy and abs((float(msg.get("vSolInBondingCurve") or 0) - sol) - INITIAL_VSOL) < 0.01:
            t = new_tok(mint, now)
            COUNTERS["implicit_creates"] += 1
        else:
            COUNTERS["trade_untracked"] += 1
            return None

    w = msg.get("traderPublicKey")
    tk = float(msg.get("tokenAmount") or 0)
    t.last = now
    t.trades += 1
    if msg.get("creator"):
        t.creator = msg["creator"]
    mcap = float(msg.get("marketCapSol") or 0)
    if mcap > 0:
        t.mcap = mcap
    if msg.get("price"):
        t.price = float(msg["price"])
    if "realTok" in msg and msg["realTok"] <= 0:
        t.complete = True

    fee = msg.get("fee_sol")
    if fee is None:
        fee = sol * FEE_RATE_FALLBACK
        t.fees_est += fee
        COUNTERS["fees_estimated"] += 1
    else:
        COUNTERS["fees_measured"] += 1
    t.fees += fee

    if is_buy:
        t.buys += 1
        t.buy_sol += sol
        t.buyers.add(w)
        set_balance(t, w, t.bal.get(w, 0.0) + tk)
    else:
        t.sells += 1
        t.sell_sol += sol
        set_balance(t, w, t.bal.get(w, 0.0) - tk)
        if t.creator and w == t.creator:
            t.dev_sold = True
    return t

# ============================================================
# AUTO TRADE ENGINE
# ============================================================

class SimFailed(Exception):
    pass

async def rpc_call(method, params, timeout=15):
    errors = []
    for rpc in HTTP_RPCS:
        try:
            async with aiohttp.ClientSession() as s:
                body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                async with s.post(rpc, json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    data = await r.json()
            if "error" in data:
                err = data["error"]
                errors.append(f"{urlparse(rpc).netloc}: {scrub(json.dumps(err))[:200]}")
                if method == "sendTransaction" and isinstance(err, dict) and err.get("code") == -32002:
                    raise SimFailed(scrub(json.dumps(err))[:700])
                continue
            return data.get("result")
        except SimFailed:
            raise
        except Exception as e:
            errors.append(f"{urlparse(rpc).netloc}: {scrub(e)[:200]}")
    raise RuntimeError("All RPCs failed -> " + " | ".join(errors))

async def build_signed_tx(action, mint, amount, in_sol, slippage, priority_fee, pool="auto"):
    payload = {"publicKey": OUR_PUB, "action": action, "mint": mint, "amount": amount,
               "denominatedInSol": "true" if in_sol else "false",
               "slippage": slippage, "priorityFee": priority_fee, "pool": pool}
    async with aiohttp.ClientSession() as s:
        async with s.post(PUMPPORTAL_TRADE_LOCAL, data=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise RuntimeError(f"PumpPortal {r.status}: {(await r.text())[:300]}")
            content = await r.read()
    unsigned = VersionedTransaction.from_bytes(content)
    signed = VersionedTransaction(unsigned.message, [KEYPAIR])
    return base64.b64encode(bytes(signed)).decode()

LAST_TX = {"err": ""}

async def send_and_confirm(tx_b64, skip_preflight=True, timeout=25):
    sig = await rpc_call("sendTransaction", [tx_b64, {
        "encoding": "base64", "skipPreflight": skip_preflight, "maxRetries": 5,
        "preflightCommitment": "processed"}])
    deadline = time.time() + timeout
    LAST_TX["err"] = f"المعاملة ما تأكدتش فـ {timeout}s"
    while time.time() < deadline:
        await asyncio.sleep(1)
        try:
            res = await rpc_call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            st = (res or {}).get("value", [None])[0]
            if st:
                if st.get("err"):
                    LAST_TX["err"] = "فشلات على الشبكة: " + json.dumps(st["err"])[:300]
                    return sig, False
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return sig, True
        except Exception:
            pass
    return sig, False

async def get_token_balance(mint):
    try:
        res = await rpc_call("getTokenAccountsByOwner", [OUR_PUB, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0.0
        for acc in (res or {}).get("value", []):
            total += float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0)
        return total
    except Exception:
        return None

async def get_sol_balance():
    try:
        res = await rpc_call("getBalance", [OUR_PUB])
        return (res or {}).get("value", 0) / 1e9
    except Exception:
        return None

def curve_address(mint):
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                           Pubkey.from_string(PUMP_PROGRAM))[0])

def pool_address_derived(mint):
    m = Pubkey.from_string(mint)
    auth = Pubkey.find_program_address([b"pool-authority", bytes(m)], Pubkey.from_string(PUMP_PROGRAM))[0]
    return str(Pubkey.find_program_address(
        [b"pool", (0).to_bytes(2, "little"), bytes(auth), bytes(m), bytes(Pubkey.from_string(WSOL_MINT))],
        Pubkey.from_string(PUMP_AMM_PROGRAM))[0])

async def fetch_curve(mint):
    res = await rpc_call("getAccountInfo", [curve_address(mint), {"encoding": "base64"}])
    val = (res or {}).get("value")
    if not val:
        return None
    raw = base64.b64decode(val["data"][0])
    if len(raw) < 49:
        return None
    vt, vs, rt, rs, _sup = struct.unpack_from("<5Q", raw, 8)
    complete = bool(raw[48])
    price = (vs / 1e9) / (vt / 1e6) if vt > 0 else 0.0
    return price, complete

async def fetch_dex_info(mint):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return None
                d = await r.json()
        pairs = [p for p in (d.get("pairs") or [])
                 if (p.get("baseToken") or {}).get("address") == mint
                 and (p.get("quoteToken") or {}).get("symbol", "").upper() in ("SOL", "WSOL")
                 and p.get("priceNative")]
        if not pairs:
            return None
        pairs.sort(key=lambda p: ((p.get("dexId") == "pumpswap"), (p.get("liquidity") or {}).get("usd") or 0),
                   reverse=True)
        return float(pairs[0]["priceNative"]), pairs[0].get("pairAddress"), pairs[0].get("dexId")
    except Exception:
        return None

async def _owner_token_ui(owner, mint):
    res = await rpc_call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}], timeout=10)
    total = 0.0
    for acc in (res or {}).get("value", []):
        total += float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0)
    return total

async def fetch_pool_price(mint, pos):
    pool = pos.get("pool")
    if not pool:
        try:
            pool = pool_address_derived(mint)
        except Exception:
            return None
    try:
        base, quote = await asyncio.gather(_owner_token_ui(pool, mint), _owner_token_ui(pool, WSOL_MINT))
    except Exception:
        return None
    if base <= 0 or quote <= 0:
        return None
    price = quote / base
    ref = pos.get("last_price") or pos.get("entry_price") or 0
    if ref > 0 and not (0.05 <= price / ref <= 20):
        return None
    pos["pool"] = pool
    return price

def mark_migrated(mint, why):
    pos = positions.get(mint)
    if not pos or pos.get("migrated"):
        return
    pos["migrated"] = True
    pos["migrated_at"] = time.time()
    print(f"🎓 {mint[:8]} graduated ({why}) -> tracking PumpSwap pool")
    asyncio.get_event_loop().create_task(tg(
        f"🎓 <b>GRADUATED → PumpSwap</b>\n<code>{mint}</code>\n"
        f"كنتبعو السعر من الـ pool وغادي نبيعو من pump-amm.", mint))

def position_on_trade(msg, now):
    pos = positions.get(msg.get("mint"))
    if not pos:
        return
    price = float(msg.get("price") or 0)
    if msg.get("traderPublicKey") == OUR_PUB:
        if msg.get("txType") == "buy" and not pos.get("entry_exact") and msg.get("tokenAmount"):
            fill = float(msg["solAmount"]) / float(msg["tokenAmount"])
            if fill > 0:
                pos["entry_price"] = fill
                pos["entry_exact"] = True
        return
    pos["last_event"] = now
    if "realTok" in msg and msg["realTok"] <= 0:
        mark_migrated(msg["mint"], "curve emptied")
    if price > 0:
        pos["last_price"] = price
        check_position(msg["mint"])

def check_position(mint):
    pos = positions.get(mint)
    if (not pos or pos["status"] != "open" or pos["selling"] or time.time() < pos.get("next_try", 0)):
        return
    entry, price = pos.get("entry_price") or 0, pos.get("last_price") or 0
    if entry <= 0 or price <= 0:
        return
    chg = (price / entry - 1.0) * 100.0
    peak = max(pos.get("peak_price") or entry, entry, price)
    pos["peak_price"] = peak
    reason = None
    if TAKE_PROFIT_PCT > 0 and chg >= TAKE_PROFIT_PCT:
        reason = "TAKE PROFIT"
    else:
        sl_level = entry * (1 - STOP_LOSS_PCT / 100.0)
        tr_level = peak * (1 - TRAILING_STOP_PCT / 100.0) if TRAILING_STOP_PCT > 0 else 0.0
        if price <= max(sl_level, tr_level):
            reason = "TRAILING STOP" if tr_level > sl_level else "STOP LOSS"
    if reason:
        pos["selling"] = True
        pos["trigger_time"] = time.time()
        pos["trigger_price"] = price
        asyncio.get_event_loop().create_task(execute_sell(mint, reason))

async def position_watchdog():
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_S)
        for mint, pos in list(positions.items()):
            if pos["status"] != "open" or pos["selling"]:
                continue
            fresh = time.time() - pos.get("last_event", 0) < PRICE_STALE_S
            if fresh and not pos.get("migrated"):
                continue
            price = None
            try:
                if not pos.get("migrated"):
                    cv = await fetch_curve(mint)
                    if cv:
                        price, complete = cv
                        if complete:
                            mark_migrated(mint, "curve complete flag")
                            price = None
                if pos.get("migrated"):
                    price = await fetch_pool_price(mint, pos)
                    src = "pool"
                    if not price:
                        info = await fetch_dex_info(mint)
                        if info:
                            price = info[0]
                            src = f"dex/{info[2]}"
                            if info[1] and info[2] == "pumpswap":
                                pos["pool"] = pos.get("pool") or info[1]
                    pos["price_src"] = src
            except Exception as e:
                print(f"⚠️ watchdog {mint[:6]}: {scrub(e)[:150]}")
                continue
            if not price:
                continue
            ref = pos.get("entry_price") or 0
            if ref > 0 and not (0.001 <= price / ref <= 100):
                continue
            pos["last_price"] = price
            pos["last_event"] = time.time() if not pos.get("migrated") else pos.get("last_event", 0)
            check_position(mint)

async def execute_buy(t):
    mint = t.mint
    if not AUTO_TRADE_ENABLED or mint in BOUGHT or mint in positions:
        return
    if t.complete and not ALLOW_BUY_AFTER_GRAD:
        return
    if not DRY_RUN and not KEYPAIR:
        return
    while BUY_TIMES and time.time() - BUY_TIMES[0] > 3600:
        BUY_TIMES.popleft()
    open_count = sum(1 for p in positions.values() if p["status"] in ("buying", "open"))
    if open_count >= MAX_OPEN_POSITIONS or len(BUY_TIMES) >= MAX_BUYS_PER_HOUR:
        COUNTERS["buys_skipped_limits"] += 1
        return
    BOUGHT.add(mint)
    BUY_TIMES.append(time.time())
    pos = {"status": "buying", "selling": False, "entry_price": t.price, "last_price": t.price,
           "last_event": time.time(), "opened_at": time.time(), "migrated": bool(t.complete),
           "symbol": t.symbol or mint[:6]}
    positions[mint] = pos
    tag = "🧪 [DRY RUN] " if DRY_RUN else ""
    try:
        if DRY_RUN:
            sig, ok = "DRY_RUN", True
        else:
            pool = "auto"
            tx = await build_signed_tx("buy", mint, BUY_AMOUNT_SOL, True, BUY_SLIPPAGE_PCT,
                                       BUY_PRIORITY_FEE_SOL, pool)
            sig, ok = await send_and_confirm(tx, skip_preflight=True, timeout=BUY_CONFIRM_SECONDS)
            if not ok:
                bal = await get_token_balance(mint)
                ok = bool(bal and bal > 0)
        if ok:
            pos["status"] = "open"
            COUNTERS["buys"] += 1
            e = pos["entry_price"]
            await tg(
                f"🟢 <b>{tag}AUTO BUY EXECUTED</b>\n\n"
                f"🪙 <b>{html.escape(t.name or t.symbol or '')}</b> <code>{mint}</code>\n"
                f"💰 Amount: <b>{BUY_AMOUNT_SOL} SOL</b>\n"
                f"👥 Holders: {t.holders} | 💸 Fees: {t.fees:.2f} SOL\n"
                f"📈 Entry MCAP ≈ {e * SUPPLY:.0f} SOL\n"
                f"🎯 TP: {'+%.0f%%' % TAKE_PROFIT_PCT if TAKE_PROFIT_PCT > 0 else 'off'} | 🛑 SL: -{STOP_LOSS_PCT:.0f}% | 📉 Trailing: {TRAILING_STOP_PCT:.0f}%\n"
                f"{'🎓 already graduated (PumpSwap)' + chr(10) if t.complete else ''}"
                f"🔗 <a href=\"https://solscan.io/tx/{sig}\">Tx</a>", mint)
        else:
            positions.pop(mint, None)
            await tg(f"🔴 <b>AUTO BUY FAILED</b>\n<code>{mint}</code>\n"
                     f"<b>السبب:</b> <code>{html.escape(LAST_TX['err'])}</code>", mint)
    except Exception as e:
        positions.pop(mint, None)

async def execute_sell(mint, reason):
    pos = positions.get(mint)
    if not pos:
        return
    if not pos.get("migrated") and not DRY_RUN:
        try:
            cv = await asyncio.wait_for(fetch_curve(mint), 3)
            if cv and cv[1]:
                mark_migrated(mint, "pre-sell curve check")
        except Exception:
            pass
    sig, ok = None, False
    try:
        if DRY_RUN:
            sig, ok = "DRY_RUN", True
        else:
            for attempt in range(1, SELL_RETRIES + 1):
                preferred = "pump-amm" if pos.get("migrated") else "auto"
                pool = (("auto" if preferred == "pump-amm" else "pump-amm")
                        if pos.get("last_pool") == preferred else preferred)
                pos["last_pool"] = pool
                try:
                    tx = await build_signed_tx("sell", mint, "100%", False, SELL_SLIPPAGE_PCT,
                                               SELL_PRIORITY_FEE_SOL, pool)
                    sig, ok = await send_and_confirm(tx, skip_preflight=False, timeout=SELL_CONFIRM_SECONDS)
                    if ok:
                        break
                    pos["last_err"] = f"المعاملة ما تأكدتش أو فشلات: {sig}"
                except Exception as e:
                    pos["last_err"] = f"[pool={pool}] {scrub(e)}"[:700]
                    if any(k in str(e).lower() for k in ("bondingcurvecomplete", "6005")):
                        mark_migrated(mint, "sell error 6005")
                bal = await get_token_balance(mint)
                if bal is not None and bal == 0:
                    ok = True
                    break
                await asyncio.sleep(0.3)
    except Exception as e:
        pass

    sig = sig or "unknown"
    entry = pos.get("entry_price") or 0
    trig = pos.get("trigger_price") or 0
    last = pos.get("last_price") or trig
    pct = lambda p: ((p / entry - 1) * 100.0) if entry > 0 and p > 0 else 0.0
    if ok:
        positions.pop(mint, None)
        COUNTERS["sells"] += 1
        tag = "🧪 [DRY RUN] " if DRY_RUN else ""
        icon = "✅" if (reason == "TAKE PROFIT" or pct(pos.get("peak_price") or 0) > 0 and pct(trig) > 0) else "🛑"
        await tg(
            f"{icon} <b>{tag}AUTO SELL - {reason}</b>\n\n"
            f"🪙 <code>{mint}</code>\n"
            f"🎯 Trigger: <b>{pct(trig):+.1f}%</b> | آخر سعر: {pct(last):+.1f}%\n"
            f"🔗 <a href=\"https://solscan.io/tx/{sig}\">Tx</a>", mint)
        return
    pos["fails"] = pos.get("fails", 0) + 1
    err = html.escape(str(pos.get("last_err", "unknown")))[:600]
    if pos["fails"] >= MAX_SELL_ROUNDS:
        positions.pop(mint, None)
        await tg(f"⛔ <b>AUTO SELL GAVE UP ({reason})</b>\n<code>{mint}</code>\nبيعه يدويا.", mint)
        return
    await asyncio.sleep(SELL_ROUND_COOLDOWN_SECONDS)
    if mint in positions:
        await execute_sell(mint, reason)

# ============================================================
# FILTER + ALERT
# ============================================================

def check(t, now):
    if t.state != "watching" or now < t.retry_at:
        return False
    if t.fees > MAX_FEES_SOL:
        EXITED.append((t.symbol or t.mint[:6], t.holders, t.max_holders, t.fees))
        COUNTERS["exited_fee_window"] += 1
        if t.max_holders <= MIN_HOLDERS:
            COUNTERS["exited_with_low_holders"] += 1
        toks.pop(t.mint, None)
        return False
    if t.fees < MIN_FEES_SOL:
        return False
    return t.holders > MIN_HOLDERS

async def verify_holders(mint):
    if not (VERIFY_HOLDERS and HELIUS_HTTP):
        return None
    owners = set()
    try:
        async with aiohttp.ClientSession() as s:
            for page in (1, 2, 3):
                body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccounts",
                        "params": {"mint": mint, "page": page, "limit": 1000}}
                async with s.post(HELIUS_HTTP, json=body, timeout=aiohttp.ClientTimeout(total=12)) as r:
                    data = await r.json()
                accs = (data.get("result") or {}).get("token_accounts")
                if accs is None:
                    return None
                for a in accs:
                    if float(a.get("amount") or 0) > 0:
                        owners.add(a.get("owner"))
                if len(accs) < 1000:
                    break
        return len(owners)
    except Exception:
        return None

async def dex_name(mint):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                             timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status != 200:
                    return None
                d = await r.json()
        for p in d.get("pairs") or []:
            b = p.get("baseToken") or {}
            if b.get("address") == mint and b.get("name"):
                return b.get("name"), b.get("symbol") or ""
    except Exception:
        pass
    return None

def build_alert(t, now):
    hold_sorted = sorted((b for w, b in t.bal.items() if w != t.creator and b > DUST_TOKENS), reverse=True)
    top10 = sum(hold_sorted[:10]) / SUPPLY * 100.0
    dev_pct = (t.bal.get(t.creator, 0.0) / SUPPLY * 100.0) if t.creator else None
    vol = t.buy_sol + t.sell_sol
    bp = (t.buy_sol / vol * 100.0) if vol > 0 else 0.0
    age = int(now - t.t0)
    mcap_usd = t.mcap * sol_usd()
    est_share = (t.fees_est / t.fees * 100.0) if t.fees > 0 else 0.0
    fee_src = "measured ✅" if est_share < 1 else (f"{100 - est_share:.0f}% measured" if est_share < 99 else "estimated ~")
    hold_txt = (f"<b>{t.holders}</b> tracked" if t.verified is None
                else f"<b>{t.verified}</b> verified ✅ ({t.holders} tracked)")
    dev_txt = "unknown" if dev_pct is None else f"{dev_pct:.1f}% ({'sold ⚠️' if t.dev_sold else 'no sells'})"
    title = html.escape(t.name) if t.name else "(name unknown)"
    return (
        f"🔔 <b>PUMP.FUN — HOLDERS + FEES MATCH</b>\n\n"
        f"<b>{title}</b> (${html.escape(t.symbol)})\n"
        f"<code>{t.mint}</code>\n\n"
        f"👥 Holders: {hold_txt}  (filter &gt; {MIN_HOLDERS})\n"
        f"💸 Trading fees: <b>{t.fees:.2f} SOL</b> [{fee_src}]  (filter {MIN_FEES_SOL}–{MAX_FEES_SOL})\n"
        f"💰 MCAP: <b>{fmt_usd(mcap_usd)}</b> ({t.mcap:.0f} SOL)\n"
        f"⏱ Age: {age // 60}m {age % 60:02d}s\n\n"
        f"🔄 Trades: {t.trades} | Unique buyers: {len(t.buyers)} | Volume: {vol:.1f} SOL\n"
        f"📈 Buy pressure: {bp:.0f}%\n"
        f"🧱 Top10 holders (ex-creator): {top10:.1f}%\n"
        f"👨‍💻 Creator holds: {dev_txt}\n\n"
        f"<i>Alert only — not financial advice.</i>"
    )

async def finalize_candidate(t):
    now = time.time()
    t.state = "verifying"
    hold = await verify_holders(t.mint)
    if hold is not None:
        t.verified = hold
        if hold <= MIN_HOLDERS:
            COUNTERS["verify_rejected"] += 1
            t.state = "watching"
            t.retry_at = time.time() + 90
            return False
    while ALERT_TIMES and time.time() - ALERT_TIMES[0] > 3600:
        ALERT_TIMES.popleft()
    if len(ALERT_TIMES) >= MAX_ALERTS_PER_HOUR:
        t.state = "alerted"
        COUNTERS["alerts_rate_limited"] += 1
        return True
    if not t.name:
        info = await dex_name(t.mint)
        if info:
            t.name, t.symbol = info[0][:40], info[1][:20]
    t.state = "alerted"
    ALERT_TIMES.append(time.time())
    COUNTERS["alerts"] += 1
    await tg(build_alert(t, time.time()), t.mint)
    return True

async def on_match(t):
    buy_task = None
    if AUTO_TRADE_ENABLED and not VERIFY_BEFORE_BUY:
        buy_task = asyncio.get_event_loop().create_task(execute_buy(t))
    ok = await finalize_candidate(t)
    if AUTO_TRADE_ENABLED and VERIFY_BEFORE_BUY and ok:
        await execute_buy(t)
    if buy_task:
        await buy_task

def handle_msg(msg, now=None):
    tx = msg.get("txType")
    if tx == "create":
        on_create(msg, now)
        return
    if tx not in ("buy", "sell"):
        return
    t = on_trade(msg, now)
    if not t:
        return
    now = time.time() if now is None else now
    if check(t, now):
        t.state = "verifying"
        asyncio.get_event_loop().create_task(on_match(t))

# ============================================================
# WEBSOCKET
# ============================================================

def _host(url):
    return scrub(url.split("?")[0])

async def ws_loop():
    global WS
    idx = 0
    while True:
        url = WSS_NODES[idx]
        try:
            print(f"⏳ Connecting to {_host(url)}")
            async with websockets.connect(url, ping_interval=20, ping_timeout=30, max_size=2 ** 22) as ws:
                WS = ws
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}))
                resp = json.loads(await ws.recv())
                if "error" in resp:
                    raise RuntimeError(f"logsSubscribe rejected: {scrub(resp['error'])}")
                STATE["node"] = idx
                STATE["since"] = time.time()
                LAST_MSG["t"] = time.time()
                print(f"✅ Connected ({_host(url)}). Hunting: holders > {MIN_HOLDERS}, fees {MIN_FEES_SOL}-{MAX_FEES_SOL} SOL")
                async for raw in ws:
                    LAST_MSG["t"] = time.time()
                    COUNTERS["msgs"] += 1
                    try:
                        m = json.loads(raw)
                    except Exception:
                        continue
                    if m.get("method") != "logsNotification":
                        continue
                    process_notification(m.get("params", {}).get("result", {}).get("value", {}))
        except Exception as e:
            print(f"❌ WS error: {scrub(e)[:200]}")
            idx = (idx + 1) % len(WSS_NODES)
        else:
            if STATE.pop("go_primary", False):
                idx = 0
            elif STATE.pop("rotate", False):
                idx = (idx + 1) % len(WSS_NODES)
        finally:
            WS = None
        await asyncio.sleep(3)

# ============================================================
# MAINTENANCE
# ============================================================

async def cleanup_loop():
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for m in [m for m, t in toks.items()
                  if (now - t.last > INACTIVE_S or now - t.t0 > MAX_TRACK_S) and m not in positions]:
            toks.pop(m, None)
            COUNTERS["pruned"] += 1
        if len(toks) > TOKENS_MAX:
            for m, _ in sorted(toks.items(), key=lambda kv: kv[1].last)[:len(toks) - TOKENS_MAX]:
                toks.pop(m, None)
                COUNTERS["evicted"] += 1

async def heartbeat_loop():
    while True:
        await asyncio.sleep(120)
        now = time.time()
        idle = now - LAST_MSG["t"]
        in_win = [t for t in toks.values() if MIN_FEES_SOL <= t.fees <= MAX_FEES_SOL]
        meas, est = COUNTERS["fees_measured"], COUNTERS["fees_estimated"]
        pct = meas / (meas + est) * 100 if (meas + est) else 0
        print(f"💓 SOL=${sol_usd():.0f} msgs={COUNTERS['msgs']} tracking={len(toks)} in_fee_window={len(in_win)} "
              f"alerts={COUNTERS['alerts']} buys={COUNTERS['buys']} sells={COUNTERS['sells']} open={len(positions)} idle={idle:.0f}s")
        if idle > 60 and WS is not None:
            STATE["rotate"] = True
            try:
                await WS.close()
            except Exception:
                pass

# ============================================================
# START
# ============================================================

async def main():
    print("=" * 60)
    print("PUMP.FUN HOLDERS + FEES HUNTER + AUTO BUY/SELL")
    print("=" * 60)
    bal = await get_sol_balance() if KEYPAIR else None
    mode = ("🧪 DRY RUN (محاكاة)" if DRY_RUN else "💰 LIVE") if AUTO_TRADE_ENABLED else "👀 alerts only"
    print(f"Mode: {mode} | wallet SOL balance: {bal}")
    await tg("🟢 <b>Hunter + Auto-trade started</b>\n"
             f"Mode: {mode}\n"
             f"Filters: holders &gt; {MIN_HOLDERS} · fees {MIN_FEES_SOL}–{MAX_FEES_SOL} SOL")
    await asyncio.gather(ws_loop(), sol_price_loop(), cleanup_loop(), heartbeat_loop(), position_watchdog())

if __name__ == "__main__":
    asyncio.run(main())
