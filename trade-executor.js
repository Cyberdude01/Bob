const { ClobClient, Side, OrderType } = require('@polymarket/clob-client');
const { ethers } = require('ethers');
const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();
require('dotenv').config({ path: '/etc/polymarket.env' });

// ── Config ──────────────────────────────────────────────────────────────────
const CLOB_HOST    = process.env.POLYMARKET_API_URL || 'https://clob.polymarket.com';
const CHAIN_ID     = 137; // Polygon mainnet
const SIG_TYPE     = parseInt(process.env.POLY_SIGNATURE_TYPE || '0', 10);
const SIGNALS_FILE = path.join(__dirname, 'data_exports', 'signals.json');
const TRADES_FILE  = path.join(__dirname, 'data_exports', 'trades.json');
const GAMMA_BASE   = 'https://gamma-api.polymarket.com';
const DB_PATH      = path.join(__dirname, 'market-data.db');

// ── Credential checks ────────────────────────────────────────────────────────
if (!process.env.POLY_PRIVATE_KEY) {
  console.error('[trade-executor] Missing POLY_PRIVATE_KEY in /etc/polymarket.env');
  process.exit(1);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function loadJSON(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

function saveJSON(file, data) {
  fs.writeFileSync(file, JSON.stringify(data, null, 2));
}

function nowET() {
  return new Date().toLocaleString('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  });
}

// Build a dedup key from a signal — one trade per slug+outcome per 15-min window.
// We derive a window ID from the current time truncated to 15-min boundaries.
function windowId() {
  const now = Date.now();
  return Math.floor(now / (15 * 60 * 1000));
}

function signalKey(sig) {
  return `${sig.slug}|${sig.outcome}|${windowId()}`;
}

// ── Resolve token_id + conditionId from local SQLite DB ───────────────────────
// Queries the most recent market_data row for a given symbol + outcome
// recorded in the last 5 minutes, so we always get the live 15-min window.
function resolveTokenFromDB(symbol, outcome) {
  return new Promise((resolve, reject) => {
    const db = new sqlite3.Database(DB_PATH, sqlite3.OPEN_READONLY);
    const cutoff = Math.floor(Date.now() / 1000) - 300; // last 5 min
    const sql = `
      SELECT token_id, market_id, ask
      FROM market_data
      WHERE market_name LIKE ?
        AND UPPER(outcome) = UPPER(?)
        AND timestamp > ?
      ORDER BY timestamp DESC
      LIMIT 1
    `;
    db.get(sql, [`%${symbol}%`, outcome, cutoff], (err, row) => {
      db.close();
      if (err) return reject(err);
      if (!row) return reject(new Error(`No recent DB row for ${symbol} ${outcome} (last 5 min)`));
      resolve({ tokenId: row.token_id, conditionId: row.market_id, ask: row.ask });
    });
  });
}

// ── Fetch best ask from order book ─────────────────────────────────────────────
async function fetchBestAsk(tokenId) {
  const res  = await fetch(`https://clob.polymarket.com/book?token_id=${tokenId}`);
  const book = await res.json();
  const asks = book.asks || [];
  if (asks.length === 0) return null;
  return parseFloat(asks[0].price);
}

// ── Build CLOB client (with optional API-key derivation) ─────────────────────
async function buildClient() {
  const wallet = new ethers.Wallet(process.env.POLY_PRIVATE_KEY);

  // Use explicit API creds if available, otherwise derive from private key
  let creds;
  if (process.env.POLY_API_KEY && process.env.POLY_API_SECRET && process.env.POLY_API_PASSPHRASE) {
    creds = {
      key:        process.env.POLY_API_KEY,
      secret:     process.env.POLY_API_SECRET,
      passphrase: process.env.POLY_API_PASSPHRASE,
    };
    console.log('[trade-executor] Using existing CLOB API credentials.');
  } else {
    console.log('[trade-executor] No CLOB API creds found — deriving from private key…');
    // Build a temporary client (L1 only) to derive credentials
    const tmpClient = new ClobClient(CLOB_HOST, CHAIN_ID, wallet);
    creds = await tmpClient.createOrDeriveApiKey();
    console.log(`[trade-executor] Derived API key: ${creds.key}`);
    // Append active creds to env file so future runs skip derivation
    try {
      const append = `\n# Auto-derived by trade-executor\nPOLY_API_KEY=${creds.key}\nPOLY_API_SECRET=${creds.secret}\nPOLY_API_PASSPHRASE=${creds.passphrase}\n`;
      fs.appendFileSync('/etc/polymarket.env', append);
      console.log('[trade-executor] Appended derived creds to /etc/polymarket.env');
    } catch (e) {
      console.warn('[trade-executor] Could not save creds to env file:', e.message);
    }
  }

  // SIG_TYPE 1 = POLY_PROXY — pass POLY_ADDRESS as funderAddress
  const funder = SIG_TYPE === 1 ? process.env.POLY_ADDRESS : undefined;
  return new ClobClient(CLOB_HOST, CHAIN_ID, wallet, creds, SIG_TYPE, funder);
}

// ── Execute a single signal ────────────────────────────────────────────────────
async function executeTrade(client, sig) {
  const { symbol, slug, outcome, size, trigger, confidence } = sig;

  // 1. Resolve token_id directly from the local DB (no external API call needed)
  const resolved = await resolveTokenFromDB(symbol, outcome);
  const tokenId  = resolved.tokenId;

  // 2. Get current best ask from CLOB order book
  const bestAsk = await fetchBestAsk(tokenId);
  if (!bestAsk) {
    console.warn(`[SKIP] ${slug} ${outcome} — no asks in order book`);
    return null;
  }

  // 4. Build and post a GTC limit order at the ask (fills immediately if liquidity exists)
  const orderArgs = {
    tokenID: tokenId,
    price:   bestAsk,
    side:    Side.BUY,
    size:    parseFloat(size),
  };

  console.log(`[ORDER] ${slug} ${outcome} — BUY ${size} USDC @ ${bestAsk} (${trigger}, conf=${confidence})`);

  const signedOrder = await client.createOrder(orderArgs);
  const response    = await client.postOrder(signedOrder, OrderType.GTC);

  return {
    timestamp:  nowET(),
    slug,
    outcome,
    trigger,
    confidence,
    tokenId,
    side:       'BUY',
    size:       parseFloat(size),
    price:      bestAsk,
    orderId:    response.orderID || response.order_id || null,
    status:     response.status  || 'submitted',
    raw:        response,
  };
}

// ── Main ───────────────────────────────────────────────────────────────────────
async function main() {
  const sigFile = loadJSON(SIGNALS_FILE);
  if (!sigFile || !Array.isArray(sigFile.data) || sigFile.data.length === 0) {
    console.log('[trade-executor] No signals to act on.');
    return;
  }

  // Load existing trades to avoid duplicates within the same 15-min window
  const tradesFile = loadJSON(TRADES_FILE) || { updated: '', data: [] };
  const executedKeys = new Set((tradesFile.data || []).map(t => t._key).filter(Boolean));

  // Deduplicate signals: latest unique (slug + outcome) per window
  const seen = new Map();
  for (const sig of sigFile.data) {
    const k = signalKey(sig);
    if (!seen.has(k)) seen.set(k, sig); // first occurrence = most recent
  }

  const candidates = [...seen.entries()].filter(([k]) => !executedKeys.has(k));
  if (candidates.length === 0) {
    console.log('[trade-executor] All signals already executed this window — nothing to do.');
    return;
  }

  console.log(`[trade-executor] ${candidates.length} signal(s) to execute…`);
  const client = await buildClient();

  for (const [key, sig] of candidates) {
    try {
      const trade = await executeTrade(client, sig);
      if (trade) {
        trade._key = key;
        tradesFile.data.unshift(trade);
        console.log(`[DONE]  ${sig.slug} ${sig.outcome} — orderId=${trade.orderId} status=${trade.status}`);
      }
    } catch (err) {
      console.error(`[ERROR] ${sig.slug} ${sig.outcome} — ${err.message}`);
    }
    // Brief pause between orders
    await new Promise(r => setTimeout(r, 500));
  }

  tradesFile.updated = nowET();
  saveJSON(TRADES_FILE, tradesFile);
  console.log(`[trade-executor] Done. Trades file updated.`);
}

main().catch(err => {
  console.error('[trade-executor] Fatal:', err.message);
  process.exit(1);
});
