"""
Stock Market Game connector for Claude (remote MCP server).

Logs into stockmarketgame.org with YOUR account and lets Claude:
  - read your account summary, holdings, pending orders, history, gains/losses, rankings
  - preview a trade (the site's own VERIFY step - never places anything)
  - place a trade only after a preview (the site's TRADE step)
  - cancel pending orders
  - look up live stock quotes (Yahoo Finance) to help pick trades

Settings come from environment variables (set them in your host's dashboard, never in code):
  SMG_USERNAME      your Stock Market Game username
  SMG_PASSWORD      your Stock Market Game password
  CONNECTOR_SECRET  a long random string; it becomes part of the connector URL so strangers can't use it
  PORT              set automatically by most hosts (defaults to 8000)
"""

import os
import re
import secrets
import time
import datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
BASE = "https://www.stockmarketgame.org/"
USERNAME = os.environ.get("SMG_USERNAME", "").strip().upper()  # the site upper-cases usernames
PASSWORD = os.environ.get("SMG_PASSWORD", "")
def _find_secret() -> str:
    """Find CONNECTOR_SECRET, forgiving extra spaces, lowercase, or the CONNECTER spelling."""
    for key, value in os.environ.items():
        norm = key.strip().upper().replace(" ", "")
        if norm in ("CONNECTOR_SECRET", "CONNECTER_SECRET"):
            return value.strip()
    return ""


SECRET = _find_secret()
PORT = int(os.environ.get("PORT", "8000"))
# Your advisor website, allowed to call this server from the browser
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://keerthanpadarthi-sys.github.io").rstrip("/")

if len(SECRET) < 20 or "/" in SECRET:
    print("---- CONNECTOR_SECRET problem ----", flush=True)
    related = [k for k in os.environ if any(w in k.upper() for w in ("SECRET", "CONNECT", "SMG"))]
    if not related:
        print("No settings named like CONNECTOR_SECRET / SMG_* were found at all.", flush=True)
    for k in related:
        v = os.environ[k].strip()
        print(f"Found setting {k!r}: value is {len(v)} characters long"
              + (" (contains '/', not allowed)" if "/" in v else ""), flush=True)
    raise SystemExit("Set CONNECTOR_SECRET to a random string of at least 20 characters "
                     "(letters and numbers only).")

# Data feeds, copied from the site's own js/Common.js
URLS = {
    "login": "cgi-bin/hailogin",
    "ping": "cgi-bin/haipage/page.html?tpl=xmltpl/pingxml",
    "account": "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/cont_acctsum&toggle=TRUE&cyear={y}&cmonth={m}&cday={d}",
    "holdings": "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/cont_acctholdings",
    "pending": "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/cont_pendingtrans",
    "history": "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/cont_transhistory2",
    "gainloss": "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/cont_gainsloss",
    "rankings": "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/pa_xml",
    "cancel": "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/cont_entertrade_cancel&LogId={logid}&password={pw}",
    "trade": (
        "cgi-bin/haipage/page.html?tpl=Administration/game/a_trad/app_verifyandtrade&action={action}"
        "&eqtype={eqtype}&OrderTypeId={ordertypeid}&SymbolName={symbol}&OrderType={ordertype}"
        "&BuySellAmt={amount}&LimitPrice={limit}"
    ),
    "trade_app": "js/components/trade-entry-app/build/index.html",
}

# ---------------------------------------------------------------------------
# Trade codes. The order form is a separate app, so these values are a best
# guess until confirmed. Previews are safe to try: VERIFY never places a trade.
# If previews come back with an error, run the `find_trade_codes` tool and
# update these (or ask Claude which values to use).
# ---------------------------------------------------------------------------
TRADE_CODES = {
    "eqtype": os.environ.get("SMG_EQTYPE", "STOCK"),
    "actions": {  # what you ask for -> what the site calls it (OrderTypeId)
        "buy": os.environ.get("SMG_CODE_BUY", "BUY"),
        "sell": os.environ.get("SMG_CODE_SELL", "SELL"),
        "short": os.environ.get("SMG_CODE_SHORT", "SHORT"),
        "cover": os.environ.get("SMG_CODE_COVER", "COVER"),
    },
    "order_types": {  # OrderType
        "market": os.environ.get("SMG_CODE_MARKET", "MARKET"),
        "limit": os.environ.get("SMG_CODE_LIMIT", "LIMIT"),
        "stop": os.environ.get("SMG_CODE_STOP", "STOP"),
    },
}

PREVIEW_TTL = 600  # a preview stays valid for 10 minutes


# ---------------------------------------------------------------------------
# Site session
# ---------------------------------------------------------------------------
class SMGSession:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=BASE,
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (SMG connector)"},
        )
        self.logged_in = False

    async def login(self):
        if not USERNAME or not PASSWORD:
            raise RuntimeError("SMG_USERNAME and SMG_PASSWORD are not set on the server.")
        self.client.cookies.clear()
        r = await self.client.post(
            URLS["login"],
            data={"ACCOUNTNO": USERNAME, "USER_PIN": PASSWORD, "SECURITY_STRING": ""},
        )
        if not self.client.cookies.get("sid"):
            raise RuntimeError(
                "Login failed (no session cookie). Check SMG_USERNAME / SMG_PASSWORD. "
                f"Site answered HTTP {r.status_code} at {r.url}."
            )
        self.logged_in = True

    async def get(self, path: str) -> str:
        """Fetch a feed, logging in (again) whenever the session looks expired."""
        for attempt in range(2):
            if not self.logged_in:
                await self.login()
            r = await self.client.get(path)
            text = r.text
            expired = (
                "invalidlogin" in str(r.url).lower()
                or "session timeout" in text.lower()
                or ("xmldataisland" not in text.lower() and "<xml" not in text.lower())
            )
            if not expired or attempt == 1:
                return text
            self.logged_in = False
        return text


smg = SMGSession()


# ---------------------------------------------------------------------------
# XML helpers (same idea as the site's GetXMLFromHtmlResponse)
# ---------------------------------------------------------------------------
def extract_xml(html: str):
    m = re.search(r'<xml\s+id="xmldataisland">.*?</xml>', html, re.S | re.I)
    if not m:
        return None
    s = m.group(0)
    s = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", s)
    s = re.sub(r"<(/?)XML", r"<\1xml", s)
    try:
        return ET.fromstring(s)
    except ET.ParseError:
        return None


def to_data(el):
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    out = {}
    for k in kids:
        v = to_data(k)
        if k.tag in out:
            if not isinstance(out[k.tag], list):
                out[k.tag] = [out[k.tag]]
            out[k.tag].append(v)
        else:
            out[k.tag] = v
    return out


def plain_text(html: str, limit=1500) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


async def feed(name: str, **fmt):
    html = await smg.get(URLS[name].format(**fmt))
    root = extract_xml(html)
    if root is None:
        return {"error": "Could not read data from the site.", "page_text": plain_text(html)}
    return to_data(root)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "Stock Market Game",
    instructions=(
        "Tools for the user's Stock Market Game account (a simulation with virtual money). "
        "Before placing any trade: call preview_trade, show the user the exact order and the site's "
        "verification message, and wait for the user's explicit yes, unless the user has said in this "
        "conversation to trade without asking. Use get_quote for live prices."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=f"/{SECRET}/mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def get_account_summary() -> dict:
    """Cash balance, stock/mutual fund/bond values and total equity for the account."""
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    data = await feed("account", y=now.year, m=now.month, d=now.day)
    if "currentdate_data" not in str(data):
        # the site may count months from 0 like JavaScript does
        data = await feed("account", y=now.year, m=now.month - 1, d=now.day)
    return data


@mcp.tool()
async def get_holdings() -> dict:
    """Every position currently held: ticker, shares, prices, market value, gain/loss."""
    return await feed("holdings")


@mcp.tool()
async def get_pending_orders() -> dict:
    """Orders waiting to be filled. Each record has a `logid` used by cancel_order."""
    return await feed("pending")


@mcp.tool()
async def get_transaction_history() -> dict:
    """Past trades on the account."""
    return await feed("history")


@mcp.tool()
async def get_gains_losses() -> dict:
    """Realized gains and losses from closed positions."""
    return await feed("gainloss")


@mcp.tool()
async def get_rankings() -> dict:
    """The account's ranking and game information."""
    return await feed("rankings")


_previews: dict[str, dict] = {}


def _trade_url(action: str, p: dict, password: str = "") -> str:
    url = URLS["trade"].format(
        action=action,
        eqtype=quote(p["eqtype"]),
        ordertypeid=quote(p["ordertypeid"]),
        symbol=quote(p["symbol"]),
        ordertype=quote(p["ordertype"]),
        amount=quote(str(p["amount"])),
        limit=quote(str(p["limit"])),
    )
    if password:
        url += "&password=" + quote(password)
    return url


def _summarize(html: str):
    root = extract_xml(html)
    return to_data(root) if root is not None else {"page_text": plain_text(html)}


async def make_preview(symbol, action, shares, order_type="market", limit_price=None) -> dict:
    action = str(action).lower().strip()
    order_type = str(order_type or "market").lower().strip()
    try:
        shares = int(shares)
    except (TypeError, ValueError):
        return {"error": "shares must be a whole number"}
    if action not in TRADE_CODES["actions"]:
        return {"error": "action must be buy, sell, short, or cover"}
    if order_type not in TRADE_CODES["order_types"]:
        return {"error": "order_type must be market, limit, or stop"}
    if shares <= 0:
        return {"error": "shares must be a positive whole number"}
    if order_type != "market" and not limit_price:
        return {"error": f"{order_type} orders need a limit_price"}
    limit_price = float(limit_price) if limit_price else None

    params = {
        "eqtype": TRADE_CODES["eqtype"],
        "ordertypeid": TRADE_CODES["actions"][action],
        "symbol": str(symbol).upper().strip(),
        "ordertype": TRADE_CODES["order_types"][order_type],
        "amount": shares,
        "limit": f"{limit_price:.2f}" if limit_price else "",
    }
    html = await smg.get(_trade_url("VERIFY", params))

    pid = secrets.token_urlsafe(8)
    _previews[pid] = {"params": params, "created": time.time()}
    return {
        "preview_id": pid,
        "order": f"{action.upper()} {shares} {params['symbol']} ({order_type}"
        + (f" @ ${limit_price:.2f}" if limit_price else "")
        + ")",
        "site_verification": _summarize(html),
        "note": "Nothing has been traded yet. Call place_trade with this preview_id to submit.",
    }


async def submit_preview(preview_id: str) -> dict:
    p = _previews.pop(preview_id, None)
    if not p:
        return {"error": "Unknown or already-used preview_id. Run preview_trade again."}
    if time.time() - p["created"] > PREVIEW_TTL:
        return {"error": "That preview expired (10 minutes). Run preview_trade again."}
    html = await smg.get(_trade_url("TRADE", p["params"], PASSWORD))
    return {"submitted": p["params"], "site_response": _summarize(html)}


@mcp.tool()
async def preview_trade(
    symbol: str,
    action: str,
    shares: int,
    order_type: str = "market",
    limit_price: float | None = None,
) -> dict:
    """Check a stock order with the site WITHOUT placing it (the site's VERIFY step).

    action: buy, sell, short, or cover. order_type: market, limit, or stop.
    limit_price is required for limit and stop orders.
    Returns the site's verification plus a preview_id to pass to place_trade.
    Show the user the result and get their OK before calling place_trade.
    """
    return await make_preview(symbol, action, shares, order_type, limit_price)


@mcp.tool()
async def place_trade(preview_id: str) -> dict:
    """Place a trade that was checked with preview_trade (the site's TRADE step).
    Only call after the user has approved the preview (or told you not to ask)."""
    return await submit_preview(preview_id)


@mcp.tool()
async def cancel_order(logid: str) -> dict:
    """Cancel a pending order. Get the logid from get_pending_orders.
    Confirm with the user which order they mean before cancelling."""
    html = await smg.get(URLS["cancel"].format(logid=quote(logid), pw=quote(PASSWORD)))
    return _summarize(html)


async def yahoo_quotes(symbols: list[str]) -> dict:
    """Fetch quotes for up to 25 symbols from Yahoo Finance."""
    out = {}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for sym in symbols[:25]:
            try:
                r = await c.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym)}",
                    params={"range": "1mo", "interval": "1d"},
                )
                res = r.json()["chart"]["result"][0]
                meta = res["meta"]
                q = res["indicators"]["quote"][0]
                closes = [x for x in q["close"] if x is not None]
                price = meta.get("regularMarketPrice")
                month_start = meta.get("chartPreviousClose")
                prev_close = closes[-2] if len(closes) > 1 else month_start
                opens = [x for x in q.get("open", []) if x is not None]
                out[sym] = {
                    "symbol": sym,
                    "name": meta.get("longName") or meta.get("shortName"),
                    "price": price,
                    "prevClose": prev_close,
                    "open": opens[-1] if opens else price,
                    "high": meta.get("regularMarketDayHigh") or price,
                    "low": meta.get("regularMarketDayLow") or price,
                    "day_change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else None,
                    "month_change_pct": round((price - month_start) / month_start * 100, 2) if month_start else None,
                    "month_low": round(min(closes), 2) if closes else None,
                    "month_high": round(max(closes), 2) if closes else None,
                }
            except Exception as e:
                out[sym] = {"symbol": sym, "error": f"quote unavailable ({type(e).__name__})"}
    return out


@mcp.tool()
async def get_quote(symbols: str) -> list:
    """Live price, daily change and 1-month range for one or more tickers (comma-separated)."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return list((await yahoo_quotes(syms[:15])).values())


@mcp.tool()
async def find_trade_codes() -> dict:
    """Troubleshooting: read the site's order-form app and return the code around the trade
    parameters (eqtype, OrderTypeId, OrderType), so the correct values can be set."""
    await smg.get(URLS["ping"])  # make sure we're logged in
    page = (await smg.client.get(URLS["trade_app"])).text
    scripts = re.findall(r'src="([^"]+\.js)"', page)
    snippets = []
    for s in scripts:
        url = s if s.startswith("http") else "js/components/trade-entry-app/build/" + s.lstrip("./")
        if s.startswith("/"):
            url = s.lstrip("/")
        js = (await smg.client.get(url)).text
        for m in re.finditer(r"eqtype|OrderTypeId|OrderType|BuySellAmt", js):
            snippets.append(js[max(0, m.start() - 300): m.end() + 300])
            if len(snippets) >= 12:
                break
    return {"scripts": scripts, "current_codes": TRADE_CODES, "snippets": snippets}


# ---------------------------------------------------------------------------
# JSON API for the Stock Game Advisor website
# ---------------------------------------------------------------------------
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _money(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("(", "-").replace(")", "").strip())
    except ValueError:
        return None


def _records(data):
    """Find every dict under a 'record' key anywhere in parsed feed data."""
    found = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() == "record":
                    found.extend(v if isinstance(v, list) else [v])
                else:
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(data)
    return [r for r in found if isinstance(r, dict)]


def _pick(rec, *names, contains=()):
    low = {k.lower(): v for k, v in rec.items()}
    for n in names:
        if n in low and str(low[n]).strip():
            return low[n]
    for part in contains:
        for k, v in low.items():
            if part in k and str(v).strip():
                return v
    return None


def normalize_holdings(data) -> list:
    out = []
    for r in _records(data):
        sym = _pick(r, "ticker", "symbol", "symbol_name", contains=("ticker", "symbol"))
        shares = _money(_pick(r, "shares", "quantity", "shares_held", contains=("share", "qty", "quantity")) or "")
        if not sym or not shares:
            continue
        avg = _money(_pick(r, "purchase_price", "avg_price", "average_price", "cost_per_share",
                           contains=("purchase", "avg", "average", "cost_per")) or "")
        if avg is None:
            total_cost = _money(_pick(r, "net_cost", "total_cost", "cost_basis", contains=("cost",)) or "")
            avg = abs(total_cost) / abs(shares) if total_cost else None
        price = _money(_pick(r, "current_price", "price", "last_price", "close_price",
                             contains=("current", "market_price", "last")) or "")
        out.append({"symbol": str(sym).strip().upper(), "shares": abs(shares),
                    "short": shares < 0 or "short" in str(r).lower(),
                    "avgCost": round(avg, 4) if avg else None, "price": price})
    return out


def _cors(request: Request, resp: Response) -> Response:
    origin = request.headers.get("origin", "")
    if origin.rstrip("/") == ALLOWED_ORIGIN or ALLOWED_ORIGIN == "*":
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def api_route(path, methods):
    def deco(fn):
        async def handler(request: Request):
            if request.method == "OPTIONS":
                return _cors(request, Response(status_code=204))
            try:
                result = await fn(request)
                return _cors(request, JSONResponse(result))
            except Exception as e:
                return _cors(request, JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500))
        mcp.custom_route(f"/{SECRET}/api/{path}", methods=methods + ["OPTIONS"])(handler)
        return fn
    return deco


@api_route("ping", ["GET"])
async def api_ping(request):
    return {"ok": True, "service": "Stock Market Game connector"}


@api_route("portfolio", ["GET"])
async def api_portfolio(request):
    account = await get_account_summary()
    holdings_raw = await get_holdings()
    acct = {}
    def find_cd(x):
        if isinstance(x, dict):
            if "currentdate_data" in x:
                v = x["currentdate_data"]
                return v[0] if isinstance(v, list) else v
            for v in x.values():
                r = find_cd(v)
                if r: return r
        return None
    cd = find_cd(account) or {}
    for k, v in (cd.items() if isinstance(cd, dict) else []):
        m = _money(v)
        if m is not None:
            acct[k] = m
    cash = acct.get("cash_balance")
    total = acct.get("total_equity") or acct.get("equity") or acct.get("total_value")
    if total is None and acct:
        total = sum(acct.get(k, 0) for k in ("cash_balance", "long_stock", "mutual_funds",
                                             "treasury_bonds", "municipal_bonds", "corporate_bonds")) \
                - abs(acct.get("short_stock", 0) or 0)
    holdings = normalize_holdings(holdings_raw)
    return {"cash": cash, "totalEquity": total, "account": acct, "holdings": holdings,
            "raw": {"account": account, "holdings": holdings_raw} if request.query_params.get("raw") else None}


@api_route("quotes", ["GET"])
async def api_quotes(request):
    syms = [s.strip().upper() for s in request.query_params.get("symbols", "").split(",") if s.strip()]
    return await yahoo_quotes(syms)


@api_route("preview", ["POST"])
async def api_preview(request):
    b = await request.json()
    return await make_preview(b.get("symbol", ""), b.get("action", ""), b.get("shares", 0),
                              b.get("orderType", "market"), b.get("limitPrice"))


@api_route("trade", ["POST"])
async def api_trade(request):
    b = await request.json()
    return await submit_preview(str(b.get("previewId", "")))


if __name__ == "__main__":
    print(f"Stock Market Game connector on port {PORT}, path /<CONNECTOR_SECRET>/mcp")
    mcp.run(transport="streamable-http")
