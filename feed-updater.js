/**
 * feed-updater.js — Polymarket 15M GitHub Feed Restorer
 *
 * Fetches live market data from Polymarket APIs, computes analytics
 * from historical CSV data, updates all report files, then commits
 * and pushes to git.
 *
 * Run once:   node feed-updater.js
 * Continuous: node feed-updater.js --loop  (updates every 5 minutes)
 */
'use strict';
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
// ─── Configuration ────────────────────────────────────────────────────────────
const REPO_DIR      = path.resolve(__dirname);
const DATA_DIR      = path.join(REPO_DIR, 'data_exports');
const REPORTS_DIR   = path.join(REPO_DIR, 'reports');
const DB_PATH       = path.join(REPO_DIR, 'market-data.db');
const LOOP_INTERVAL = 1 * 60 * 1000; // 1 minute
const SYMBOLS = ['BTC', 'ETH', 'SOL']; // XRP paused — insufficient price movement
// Base slug patterns → symbol mapping (slug may have a timestamp suffix like -1773294300)
const SLUG_PREFIX_TO_SYMBOL = {
  'btc-updown-15m': 'BTC',
  'eth-updown-15m': 'ETH',
  'sol-updown-15m': 'SOL',
  'xrp-updown-15m': 'XRP',
  'btc-up-or-down-15m': 'BTC',
  'eth-up-or-down-15m': 'ETH',
  'sol-up-or-down-15m': 'SOL',
  'xrp-up-or-down-15m': 'XRP',
};
// Vol / trend thresholds (derived from historical data analysis)
const VOL_THRESHOLD   = 5.0;   // rv60 above this = HighVol
const TREND_THRESHOLD = 0.016; // eff60 above this = Trend
// Signal thresholds
const MIN_EDGE_TREND_FOLLOW = 0.12;  // |probUp - 0.5| >= 0.12 to fire trend_follow
const MIN_EDGE_DIRECTIONAL  = 0.25;  // |probUp - 0.5| >= 0.25 to fire directional (75%+ probability)
const TRADE_SIZE            = 5.0;   // Fixed $5 USDC stake per signal
const PRE_ORDER_PRICE       = 0.44;  // Fixed bid price for pre_order straddles
const MAX_ENTRY_DIRECTIONAL = 0.56;  // Max entry price for directional/trend_follow (above = negative EV at 56% win rate)
// ─── Time helpers ─────────────────────────────────────────────────────────────
function isDSTActive(date) {
  const y = date.getUTCFullYear();
  // US DST: 2nd Sunday in March
  const mar = new Date(Date.UTC(y, 2, 1));
  let sundays = 0;
  for (let d = new Date(mar); d.getUTCMonth() === 2; d.setUTCDate(d.getUTCDate() + 1)) {
    if (d.getUTCDay() === 0 && ++sundays === 2) { mar.setTime(d.getTime()); break; }
  }
  // 1st Sunday in November
  const nov = new Date(Date.UTC(y, 10, 1));
  for (let d = new Date(nov); d.getUTCMonth() === 10; d.setUTCDate(d.getUTCDate() + 1)) {
    if (d.getUTCDay() === 0) { nov.setTime(d.getTime()); break; }
  }
  return date >= mar && date < nov;
}
function toET(date = new Date()) {
  return new Date(date.getTime() + (isDSTActive(date) ? -4 : -5) * 3600000);
}
function formatET(date = new Date()) {
  const et = toET(date);
  const y  = et.getUTCFullYear();
  const mo = String(et.getUTCMonth() + 1).padStart(2, '0');
  const d  = String(et.getUTCDate()).padStart(2, '0');
  const h  = et.getUTCHours();
  const mi = String(et.getUTCMinutes()).padStart(2, '0');
  const ap = h >= 12 ? 'PM' : 'AM';
  const h12 = String(h % 12 || 12).padStart(2, '0');
  return `${y}-${mo}-${d} ${h12}:${mi} ${ap} ET`;
}
function formatETShort(date = new Date()) {
  const et = toET(date);
  const mo = String(et.getUTCMonth() + 1).padStart(2, '0');
  const d  = String(et.getUTCDate()).padStart(2, '0');
  const h  = et.getUTCHours();
  const mi = String(et.getUTCMinutes()).padStart(2, '0');
  const ap = h >= 12 ? 'PM' : 'AM';
  const h12 = String(h % 12 || 12).padStart(2, '0');
  return `2026-${mo}-${d} ${h12}:${mi} ${ap} ET`;
}
// ─── CSV helpers ──────────────────────────────────────────────────────────────
function loadCSV(symbol) {
  const file = path.join(DATA_DIR, `${symbol}.csv`);
  if (!fs.existsSync(file)) return { headers: [], rows: [] };
  const lines = fs.readFileSync(file, 'utf8').trim().split('\n');
  if (lines.length < 2) return { headers: lines[0]?.split(',') || [], rows: [] };
  const headers = lines[0].split(',');
  const rows = lines.slice(1).map(line => {
    const vals = line.split(',');
    const obj = {};
    // Handle schema difference: newer rows have extra 'slug' column at index 2
    // Header: ts,symbol,condition_id,token_id_up,...
    // New rows: ts,symbol,slug,condition_id,token_id_up,...
    if (vals.length > headers.length) {
      // Extra column — map with slug inserted
      const extended = ['ts', 'symbol', 'slug', ...headers.slice(2)];
      extended.forEach((h, i) => { obj[h] = vals[i]; });
    } else {
      headers.forEach((h, i) => { obj[h] = vals[i]; });
    }
    return obj;
  });
  return { headers, rows };
}
function appendCSVRow(symbol, row) {
  const file = path.join(DATA_DIR, `${symbol}.csv`);
  fs.appendFileSync(file, '\n' + row);
}
// ─── Analytics ────────────────────────────────────────────────────────────────
function computeVolMetrics(priceHistory) {
  if (priceHistory.length < 5) return { rv60: null, eff60: null };
  const prices = priceHistory.filter(p => p > 0.001 && p < 0.999);
  if (prices.length < 5) return { rv60: null, eff60: null };
  // Log returns
  const logRets = [];
  for (let i = 1; i < prices.length; i++) {
    if (prices[i] > 0 && prices[i - 1] > 0) {
      logRets.push(Math.log(prices[i] / prices[i - 1]));
    }
  }
  if (logRets.length < 3) return { rv60: null, eff60: null };
  // Realized volatility (annualised in "per-minute" units × 100)
  const n = logRets.length;
  const mean = logRets.reduce((a, b) => a + b, 0) / n;
  const variance = logRets.reduce((s, r) => s + (r - mean) ** 2, 0) / n;
  const rv60 = Math.sqrt(variance * n) * 100;
  // Efficiency ratio
  const totalPath = logRets.reduce((s, r) => s + Math.abs(r), 0);
  const netMove   = Math.abs(Math.log(prices[prices.length - 1] / prices[0]));
  const eff60     = totalPath > 0 ? netMove / totalPath : 0;
  return {
    rv60:  parseFloat(rv60.toFixed(5)),
    eff60: parseFloat(eff60.toFixed(3)),
  };
}
function getBuckets(rv60, eff60) {
  return {
    volBucket:   (rv60  === null || rv60  >= VOL_THRESHOLD)   ? 'HighVol' : 'LowVol',
    trendBucket: (eff60 === null || eff60 >= TREND_THRESHOLD) ? 'Trend'   : 'Range',
  };
}
// Directional probability: Bayesian shrinkage toward 0.5 at low elapsed;
// at high elapsed the market price is the best estimate.
function getDirProbs(probUp, elapsedPct, rv60) {
  if (probUp === null) return { dir60: null, dir80: null, dir90: null };
  const e = Math.min(Math.max(elapsedPct, 0), 1);
  // Weight increases with elapsed, reduced slightly by volatility
  const volPenalty = Math.min((rv60 || 5) / 50, 0.3);
  const w60 = Math.min(e * (1 - volPenalty * 0.5), 1);
  const w80 = Math.min(e * (1 - volPenalty * 0.3), 1);
  const w90 = Math.min(e * (1 - volPenalty * 0.1), 1);
  const shrink = (p, w) => parseFloat((0.5 + (p - 0.5) * w).toFixed(4));
  return {
    dir60: shrink(probUp, w60),
    dir80: shrink(probUp, w80),
    dir90: shrink(probUp, w90),
  };
}
// ─── HTTP helpers (curl-based to honour HTTPS_PROXY env var) ─────────────────
function curlGet(url, timeout = 10) {
  try {
    const out = execSync(
      `curl -s --max-time ${timeout} --compressed -L "${url}"`,
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }
    );
    return out;
  } catch {
    return null;
  }
}
function curlGetJSON(url, timeout = 10) {
  const body = curlGet(url, timeout);
  if (!body) return null;
  try { return JSON.parse(body); } catch { return null; }
}
// ─── Slug discovery ───────────────────────────────────────────────────────────
/**
 * Scrape https://polymarket.com/crypto/15M and find all active 15-minute slugs.
 * The page embeds slugs in both JSON (`"slug":"xxx-15m-TIMESTAMP"`) and
 * URL (`event/xxx-15m-TIMESTAMP`) formats.
 *
 * Returns a map of { symbol → slug } for the currently-active windows.
 */
// ─── DB slug lookup ────────────────────────────────────────────────────────────
/**
 * Query market-data.db for the most recently confirmed slug per symbol.
 * market-data-collector runs every 60s so the DB is almost always fresher
 * than the 5-minute feed-updater cycle.  Only returns slugs seen within the
 * last STALE_SECS seconds so we never serve yesterday's market.
 *
 * Uses the sqlite3 CLI (available on Debian by default) so no async code
 * is needed.  Returns {} gracefully if the DB is absent or the CLI is not
 * installed.
 */
const DB_STALE_SECS = 120; // reject slugs older than 2 minutes
function querySlugsFromDB() {
  if (!fs.existsSync(DB_PATH)) return {};
  try {
    const minTs = Math.floor(Date.now() / 1000) - DB_STALE_SECS;
    const result = {};
    for (const [prefix, sym] of [
      ['btc-updown-15m', 'BTC'],
      ['eth-updown-15m', 'ETH'],
      ['sol-updown-15m', 'SOL'],
      ['xrp-updown-15m', 'XRP'],
    ]) {
      const sql = `SELECT slug FROM market_data WHERE slug LIKE '${prefix}-%' AND timestamp > ${minTs} ORDER BY timestamp DESC LIMIT 1;`;
      const slug = execSync(`sqlite3 "${DB_PATH}" "${sql}"`,
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 3000 }).trim();
      if (slug) result[sym] = slug;
    }
    return result;
  } catch {
    return {};
  }
}
// Symbol → primary slug prefix (updown-15m variant)
const SYMBOL_TO_SLUG_PREFIX = {
  BTC: 'btc-updown-15m',
  ETH: 'eth-updown-15m',
  SOL: 'sol-updown-15m',
  XRP: 'xrp-updown-15m',
};
function discoverActiveSlugs() {
  const bySymbol = {}; // symbol → [{slug, timestamp}]
  // ── Step 0: SQLite DB (primary) ───────────────────────────────────────────
  // market-data-collector writes confirmed open slugs every 60s — far more
  // frequent than our 5-minute cycle.  Using the DB avoids all external API
  // calls for slug resolution in the common case.
  const dbSlugs = querySlugsFromDB();
  if (Object.keys(dbSlugs).length === 4) {
    console.log('  (slugs from SQLite DB)');
    return dbSlugs;
  }
  if (Object.keys(dbSlugs).length > 0) {
    console.log(`  (partial DB slugs: ${Object.keys(dbSlugs).join(', ')} — filling gaps)`);
  }
  // ── Step 1: clock-derived fast path ──────────────────────────────────────
  // For any symbols not found in the DB (e.g. at window boundaries before the
  // collector's next run), compute the expected slug from the clock and confirm
  // it via the Gamma API.  15-min windows align on exact 900-second boundaries.
  const nowSec0 = Math.floor(Date.now() / 1000);
  const windowTs = Math.floor(nowSec0 / 900) * 900; // current window start
  const result0 = Object.assign({}, dbSlugs); // start with whatever the DB gave us
  for (const sym of SYMBOLS) {
    if (result0[sym]) continue; // already resolved by DB
    const slug = `${SYMBOL_TO_SLUG_PREFIX[sym]}-${windowTs}`;
    try {
      const data = curlGetJSON(`https://gamma-api.polymarket.com/markets?slug=${slug}`, 6);
      if (Array.isArray(data) && data.length > 0 && !data[0].closed) {
        result0[sym] = slug;
      }
    } catch {}
  }
  if (Object.keys(result0).length === 4) {
    console.log('  (clock-derived slugs confirmed via Gamma)');
    return result0;
  }
  // ── Step 2: page-scrape discovery (last resort) ───────────────────────────
  try {
    const html = curlGet('https://polymarket.com/crypto/15M', 15);
    if (!html) throw new Error('empty response');
    // Extract both patterns
    const patterns = [
      /["\/]([a-z]+-updown-15m-\d+)/g,
      /["\/]([a-z]+-up-or-down-15m(?:-\d+)?)/g,
    ];
    const seen = new Set();
    for (const re of patterns) {
      let m;
      while ((m = re.exec(html)) !== null) {
        const slug = m[1];
        if (!seen.has(slug)) {
          seen.add(slug);
          // Determine symbol
          const prefix = Object.keys(SLUG_PREFIX_TO_SYMBOL).find(p => slug.startsWith(p));
          if (!prefix) continue;
          const sym = SLUG_PREFIX_TO_SYMBOL[prefix];
          // Extract timestamp suffix (0 if no suffix)
          const tsPart = slug.replace(prefix, '').replace(/^-/, '');
          const ts = parseInt(tsPart, 10) || 0;
          if (!bySymbol[sym]) bySymbol[sym] = [];
          bySymbol[sym].push({ slug, ts });
        }
      }
    }
  } catch (err) {
    console.error('  discoverActiveSlugs:', err.message);
  }
  // For each symbol, find the slug for the CURRENT window:
  // prefer the slug whose market window contains "now" (or most recently started)
  const result = {};
  const now = Date.now();
  for (const [sym, entries] of Object.entries(bySymbol)) {
    if (entries.length === 0) continue;
    if (entries.length === 1) {
      // Only accept the single entry if its window has already started
      const nowSec0 = Math.floor(Date.now() / 1000);
      if (entries[0].ts <= nowSec0) { result[sym] = entries[0].slug; continue; }
      // Otherwise fall through to the loop (will likely hit the fallback)
    }
    // Query gamma for each and pick the one with the earliest endDate that is still in future
    let best = null;
    let bestEndMs = Infinity;
    const nowSec = Math.floor(Date.now() / 1000);
    for (const { slug, ts } of entries) {
      // Skip markets that haven't started yet (future windows pre-listed on the page)
      if (ts > nowSec) continue;
      try {
        const data = curlGetJSON(
          `https://gamma-api.polymarket.com/markets?slug=${slug}`, 8);
        if (!Array.isArray(data) || data.length === 0) continue;
        const mkt = data[0];
        if (mkt.closed) continue;
        // Use CLOB question to infer window timing if possible
        const condId = mkt.conditionId;
        let windowEndMs = mkt.endDate ? new Date(mkt.endDate).getTime() : Infinity;
        // Try to get the true window from CLOB question (e.g. "March 12, 1:45AM-2:00AM ET")
        try {
          const clobData = curlGetJSON(
            `https://clob.polymarket.com/markets/${condId}`, 5);
          const q = (clobData && clobData.question) ? clobData.question : '';
          // Parse the end time from question like "March 12, 1:45AM-2:00AM ET"
          const timeMatch = q.match(/(\w+ \d+),\s+[\d:APM]+-(\d+:\d+(?:AM|PM))\s+ET/i);
          if (timeMatch) {
            const dateStr = timeMatch[1]; // e.g. "March 12"
            const endTimeStr = timeMatch[2]; // e.g. "2:00AM"
            const fullDateStr = `${dateStr} 2026 ${endTimeStr} ET`;
            const parsed = parseDateET(fullDateStr);
            if (parsed) windowEndMs = parsed;
          }
        } catch {}
        // Pick the earliest-ending future market (= current active window)
        if (windowEndMs > now && windowEndMs < bestEndMs) {
          bestEndMs = windowEndMs;
          best = slug;
        }
      } catch {}
    }
    if (!best) {
      // Fallback: pick the slug whose window has already started (ts <= now),
      // choosing the most recent one. Avoids latching onto future markets.
      const nowSec = Math.floor(Date.now() / 1000);
      const past = entries.filter(e => e.ts <= nowSec);
      best = past.length > 0
        ? past.sort((a, b) => b.ts - a.ts)[0].slug
        : entries.sort((a, b) => a.ts - b.ts)[0].slug; // absolute last resort
    }
    result[sym] = best;
  }
  // Fill any gaps with DB/clock-derived results from Steps 0–1
  for (const [sym, slug] of Object.entries(result0)) {
    if (!result[sym]) result[sym] = slug;
  }
  // Fall back to hardcoded if discovery fails
  if (Object.keys(result).length < 4) {
    console.warn('  Slug discovery incomplete, using timestamp-sorted fallback');
    const nowSec2 = Math.floor(Date.now() / 1000);
    for (const [sym, entries] of Object.entries(bySymbol)) {
      if (!result[sym] && entries.length > 0) {
        const past = entries.filter(e => e.ts <= nowSec2);
        result[sym] = past.length > 0
          ? past.sort((a, b) => b.ts - a.ts)[0].slug
          : entries.sort((a, b) => a.ts - b.ts)[0].slug;
      }
    }
  }
  return result;
}
/** Parse a human-readable ET date string to UTC ms */
function parseDateET(str) {
  // "March 12 2026 2:00AM ET" → UTC ms
  const months = {
    January:1, February:2, March:3, April:4, May:5, June:6,
    July:7, August:8, September:9, October:10, November:11, December:12,
  };
  const m = str.match(/(\w+)\s+(\d+)\s+(\d{4})\s+(\d+):(\d{2})(AM|PM)/i);
  if (!m) return null;
  const mon = months[m[1]]; if (!mon) return null;
  let h = parseInt(m[4]); const min = parseInt(m[5]);
  if (m[6].toUpperCase() === 'PM' && h !== 12) h += 12;
  if (m[6].toUpperCase() === 'AM' && h === 12) h = 0;
  // Build UTC date (convert from ET: EDT=-4, EST=-5)
  const approx = new Date(Date.UTC(parseInt(m[3]), mon-1, parseInt(m[2]), h, min));
  const offset = isDSTActive(approx) ? 4 : 5;
  return approx.getTime() + offset * 3600000;
}
// ─── Polymarket API calls ─────────────────────────────────────────────────────
function fetchMarketBySlug(slug) {
  try {
    const data = curlGetJSON(`https://gamma-api.polymarket.com/markets?slug=${slug}`);
    if (!Array.isArray(data) || data.length === 0) return null;
    const m = data[0];
    return {
      slug,
      conditionId:  m.conditionId,
      question:     m.question,
      closed:       m.closed,
      active:       m.active,
      tokenIds:     JSON.parse(m.clobTokenIds  || '[]'),
      outcomes:     JSON.parse(m.outcomes      || '[]'),
      prices:       JSON.parse(m.outcomePrices || '[]').map(Number),
      volume:       parseFloat(m.volumeNum || 0),
      startDateIso: m.startDate,
      endDateIso:   m.endDate,
    };
  } catch (err) {
    console.error(`  fetchMarketBySlug(${slug}): ${err.message}`);
    return null;
  }
}
function fetchOrderBook(tokenId) {
  try {
    const data = curlGetJSON(`https://clob.polymarket.com/book?token_id=${tokenId}`);
    if (!data) return null;
    const bids = data.bids || [];
    const asks = data.asks || [];
    const bestBid = bids.length > 0 ? parseFloat(bids[0].price) : null;
    const bestAsk = asks.length > 0 ? parseFloat(asks[0].price) : null;
    return {
      bestBid,
      bestAsk,
      spread:   (bestBid !== null && bestAsk !== null) ? parseFloat((bestAsk - bestBid).toFixed(4)) : null,
      bidDepth: bids.slice(0, 5).reduce((s, b) => s + parseFloat(b.size || 0), 0),
      askDepth: asks.slice(0, 5).reduce((s, a) => s + parseFloat(a.size || 0), 0),
    };
  } catch (err) {
    console.error(`  fetchOrderBook(${tokenId.substring(0, 12)}…): ${err.message}`);
    return null;
  }
}
function fetchTradeCounts(conditionId) {
  try {
    const data = curlGetJSON(
      `https://data-api.polymarket.com/trades?market=${conditionId}&limit=50`);
    if (!Array.isArray(data)) return { up: 0, down: 0 };
    const up   = data.filter(t => t.outcome === 'Yes'  || t.side === 'BUY').length;
    const down = data.filter(t => t.outcome === 'No'   || t.side === 'SELL').length;
    return { up, down };
  } catch {
    return { up: 0, down: 0 };
  }
}
// ─── Main data collection ─────────────────────────────────────────────────────
function collectMarketData() {
  const now   = new Date();
  const tsStr = formatET(now);
  const results = {};
  console.log(`\n[${tsStr}] Fetching market data…`);
  // Discover current active slugs
  console.log('  Discovering active 15M slugs…');
  const slugMap = discoverActiveSlugs();
  console.log('  Active slugs:', JSON.stringify(slugMap));
  for (const symbol of SYMBOLS) {
    const slug = slugMap[symbol];
    if (!slug) {
      console.log(`  ${symbol} → skipped (slug not discovered)`);
      continue;
    }
    console.log(`  ${symbol} (${slug})…`);
    const market = fetchMarketBySlug(slug);
    if (!market || market.closed) {
      console.log(`    → skipped (closed or not found)`);
      continue;
    }
    // Identify UP and DOWN token indices (outcomes: ["UP","DOWN"] or ["Yes","No"])
    const upIdx   = market.outcomes.findIndex(o => /up|yes/i.test(o));
    const downIdx = market.outcomes.findIndex(o => /down|no/i.test(o));
    if (upIdx === -1 || downIdx === -1) {
      console.log(`    → skipped (unexpected outcomes: ${market.outcomes})`);
      continue;
    }
    const upTokenId   = market.tokenIds[upIdx];
    const downTokenId = market.tokenIds[downIdx];
    const upPriceGamma   = market.prices[upIdx]   || 0.5;
    const downPriceGamma = market.prices[downIdx]  || 0.5;
    // Order books
    const upBook   = fetchOrderBook(upTokenId);
    const downBook = fetchOrderBook(downTokenId);
    // For Chainlink-settled markets the CLOB shows a flat 0.01/0.99 book.
    // Use the gamma outcomePrices as the canonical probability; fall back
    // to CLOB mid only when the spread is tight (< 0.20).
    const upClobMid = (upBook?.bestBid !== null && upBook?.bestAsk !== null)
      ? (upBook.bestBid + upBook.bestAsk) / 2 : null;
    const downClobMid = (downBook?.bestBid !== null && downBook?.bestAsk !== null)
      ? (downBook.bestBid + downBook.bestAsk) / 2 : null;
    const upClobSpread   = upBook?.spread   ?? 1;
    const downClobSpread = downBook?.spread ?? 1;
    const midUp   = (upClobSpread   < 0.2 && upClobMid   !== null) ? upClobMid   : upPriceGamma;
    const midDown = (downClobSpread < 0.2 && downClobMid !== null) ? downClobMid : downPriceGamma;
    // Market timing — use CLOB question to get the true 15-min window
    let startMs, endMs;
    const clobData = curlGetJSON(`https://clob.polymarket.com/markets/${market.conditionId}`, 5);
    const question = (clobData && clobData.question) ? clobData.question : '';
    const winMatch = question.match(/(\w+ \d+),\s+([\d:APM]+)-([\d:APM]+)\s+ET/i);
    if (winMatch) {
      const year = new Date().getUTCFullYear();
      startMs = parseDateET(`${winMatch[1]} ${year} ${winMatch[2]} ET`) || now.getTime();
      endMs   = parseDateET(`${winMatch[1]} ${year} ${winMatch[3]} ET`) || (startMs + 15 * 60000);
    } else {
      startMs = market.startDateIso ? new Date(market.startDateIso).getTime() : now.getTime();
      endMs   = startMs + 15 * 60000;
    }
    const duration = endMs - startMs;
    const elapsed  = now.getTime() - startMs;
    const elapsedPct   = Math.min(Math.max(elapsed / duration, 0), 1);
    const remainingSec = Math.max((endMs - now.getTime()) / 1000, 0);
    // ARB detection
    const arbProfit = (upPriceGamma + downPriceGamma < 0.99)
      ? parseFloat((1 - upPriceGamma - downPriceGamma).toFixed(4)) : null;
    // Historical price data for vol metrics
    const { rows: csvRows } = loadCSV(symbol);
    const recentPrices = csvRows
      .slice(-120)
      .map(r => parseFloat(r.prob_up || r.up_price || 0))
      .filter(p => p > 0.001 && p < 0.999);
    const { rv60, eff60 } = computeVolMetrics([...recentPrices, midUp]);
    const { volBucket, trendBucket } = getBuckets(rv60, eff60);
    // Previous-window momentum: detect if last window settled strongly in one direction
    // Used by momentum_carry trigger in generateSignals
    let prevWindowOutcome = null;
    const prevHighElapsed = csvRows.filter(r => parseFloat(r.elapsed_pct || 0) >= 0.85);
    if (prevHighElapsed.length >= 1) {
      const lastPrev = prevHighElapsed[prevHighElapsed.length - 1];
      const prevProbUp = parseFloat(lastPrev.prob_up || lastPrev.up_price || 0.5);
      if (prevProbUp >= 0.92)      prevWindowOutcome = 'UP';
      else if (prevProbUp <= 0.08) prevWindowOutcome = 'DOWN';
    }
    // Directional probabilities
    const { dir60, dir80, dir90 } = getDirProbs(midUp, elapsedPct, rv60);
    // Trade counts
    const trades = fetchTradeCounts(market.conditionId);
    results[symbol] = {
      symbol,
      slug,
      conditionId:    market.conditionId,
      tokenIdUp:      upTokenId,
      tokenIdDown:    downTokenId,
      upPrice:        parseFloat(upPriceGamma.toFixed(4)),
      downPrice:      parseFloat(downPriceGamma.toFixed(4)),
      upBestBid:      upBook?.bestBid   ?? null,
      upBestAsk:      upBook?.bestAsk   ?? null,
      downBestBid:    downBook?.bestBid ?? null,
      downBestAsk:    downBook?.bestAsk ?? null,
      upSpread:       upBook?.spread    ?? null,
      downSpread:     downBook?.spread  ?? null,
      upBidDepth:     upBook?.bidDepth  ?? 0,
      upAskDepth:     upBook?.askDepth  ?? 0,
      downBidDepth:   downBook?.bidDepth ?? 0,
      downAskDepth:   downBook?.askDepth ?? 0,
      elapsedPct:     parseFloat(elapsedPct.toFixed(4)),
      remainingSec:   parseFloat(remainingSec.toFixed(1)),
      arbProfit,
      upTradeCount:   trades.up,
      downTradeCount: trades.down,
      totalVolume:    parseFloat(market.volume.toFixed(5)),
      volBucket,
      trendBucket,
      rv60:  rv60  ?? 0,
      eff60: eff60 ?? 0,
      probUp: parseFloat(midUp.toFixed(4)),
      dir60pct: dir60,
      dir80pct: dir80,
      dir90pct: dir90,
      prob008: parseFloat((midUp * 0.9 + 0.05).toFixed(2)),
      prob012: parseFloat(midUp.toFixed(2)),
      prob020: parseFloat(Math.min(midUp * 1.1, 0.99).toFixed(2)),
      marketStartTs: startMs ? new Date(startMs).toISOString() : '',
      marketEndTs:   endMs   ? new Date(endMs).toISOString()   : '',
      timestamp: now.toISOString(),
      prevWindowOutcome,
    };
    console.log(`    ✓ UP=${results[symbol].upPrice} DOWN=${results[symbol].downPrice}`
      + ` elapsed=${(elapsedPct * 100).toFixed(1)}%`
      + ` ${volBucket}+${trendBucket}`);
  }
  return { results, tsStr, now };
}
// ─── CSV append ───────────────────────────────────────────────────────────────
function appendToCSVs(results, now) {
  for (const [symbol, m] of Object.entries(results)) {
    const row = [
      now.toISOString(),
      symbol,
      m.slug,
      m.conditionId,
      m.tokenIdUp,
      m.tokenIdDown,
      m.upPrice,
      m.downPrice,
      m.upBestBid   ?? '',
      m.upBestAsk   ?? '',
      m.downBestBid ?? '',
      m.downBestAsk ?? '',
      m.upSpread    ?? '',
      m.downSpread  ?? '',
      m.upBidDepth,
      m.upAskDepth,
      m.downBidDepth,
      m.downAskDepth,
      m.elapsedPct,
      m.remainingSec,
      m.arbProfit ?? '',
      m.upTradeCount,
      m.downTradeCount,
      m.totalVolume,
      m.volBucket,
      m.trendBucket,
      m.rv60,
      m.eff60,
      m.probUp,
      m.dir60pct,
      m.dir80pct,
      m.dir90pct,
      m.prob008,
      m.prob012,
      m.prob020,
      m.marketStartTs,
      m.marketEndTs,
    ].join(',');
    appendCSVRow(symbol, row);
  }
}
// ─── JSON exports ─────────────────────────────────────────────────────────────
function updateMarketsJSON(results, tsStr) {
  const data = {};
  for (const [symbol, m] of Object.entries(results)) {
    data[symbol] = {
      symbol,
      slug:         m.slug,
      condition_id: m.conditionId,
      elapsed_pct:  m.elapsedPct,
      remaining_sec: m.remainingSec,
      up_price:     m.upPrice,
      down_price:   m.downPrice,
      arb:          m.arbProfit,
      rv60:         m.rv60,
      eff60:        m.eff60,
      vol_bucket:   m.volBucket,
      trend_bucket: m.trendBucket,
      spread:       m.upSpread !== null ? parseFloat((m.upSpread * 100).toFixed(2)) : null,
      dir_60pct:    m.dir60pct,
      dir_80pct:    m.dir80pct,
      dir_90pct:    m.dir90pct,
      prob_008:     m.prob008,
      prob_012:     m.prob012,
      prob_020:     m.prob020,
    };
  }
  fs.writeFileSync(
    path.join(DATA_DIR, 'markets.json'),
    JSON.stringify({ updated: tsStr, data }, null, 2)
  );
}
// ─── Signal generation ────────────────────────────────────────────────────────
/**
 * Generate trading signals from the current market snapshot.
 *
 * Trigger logic:
 *   trend_follow     — trendBucket=Trend, 50–90% elapsed, |probUp-0.5| >= MIN_EDGE_TREND_FOLLOW
 *   directional_90pct — elapsed >= 90%, |probUp-0.5| >= MIN_EDGE_DIRECTIONAL
 *
 * Each qualifying market produces one signal (UP or DOWN, whichever has the edge).
 */
function generateSignals(results) {
  const signals = [];

  for (const [symbol, m] of Object.entries(results)) {
    const { probUp, elapsedPct, volBucket, trendBucket, rv60, eff60, slug } = m;
    const edge       = Math.abs(probUp - 0.5);
    const isUp       = probUp >= 0.5;
    const outcome    = isUp ? 'UP' : 'DOWN';
    const price      = parseFloat((isUp ? m.upPrice : m.downPrice).toFixed(4));
    const confidence = parseFloat((isUp ? probUp : 1 - probUp).toFixed(4));

    // trend_follow: Trend bucket, 50–90% elapsed, minimum edge, max entry price guard
    if (
      trendBucket === 'Trend' &&
      elapsedPct >= 0.50 &&
      elapsedPct < 0.90 &&
      edge >= MIN_EDGE_TREND_FOLLOW &&
      price <= MAX_ENTRY_DIRECTIONAL
    ) {
      signals.push({
        symbol,
        slug,
        outcome,
        side:       'BUY',
        size:       TRADE_SIZE,
        price,
        confidence,
        trigger:    'trend_follow',
        reason:     `TREND FOLLOW at ${(elapsedPct * 100).toFixed(0)}% elapsed — `
          + `P(UP)=${probUp.toFixed(3)} gives edge=${edge.toFixed(3)} toward ${outcome}. `
          + `Bucket=${volBucket}+${trendBucket} (RV60=${rv60}, Eff60=${eff60}). `
          + `Fixed $${TRADE_SIZE} USDC stake @ conf=${confidence.toFixed(3)}.`,
      });
    }

    // directional: 67%+ elapsed, any bucket, edge >= 0.25 (75%+ probability), max entry price guard
    if (elapsedPct >= 0.667 && edge >= MIN_EDGE_DIRECTIONAL && price <= MAX_ENTRY_DIRECTIONAL) {
      signals.push({
        symbol,
        slug,
        outcome,
        side:       'BUY',
        size:       TRADE_SIZE,
        price,
        confidence,
        trigger:    'directional_90pct',
        reason:     `DIRECTIONAL at ${(elapsedPct * 100).toFixed(0)}% elapsed — `
          + `P(UP)=${probUp.toFixed(3)} gives edge=${edge.toFixed(3)} toward ${outcome}. `
          + `Bucket=${volBucket}+${trendBucket} (RV60=${rv60.toFixed(5)}, Eff60=${eff60.toFixed(3)}). `
          + `Fixed $${TRADE_SIZE} USDC stake @ conf=${confidence.toFixed(3)}.`,
      });
    }

    // pre_order straddle: place simultaneous UP + DOWN bids at 0.44 on the
    // NEXT 15-min market, 5 minutes before it opens (elapsed 67–95%).
    // No directional condition — both sides always fire.  One leg will win
    // (settles ≥ $1) and one will lose ($0); the 0.44 entry price vs 0.50
    // fair value is the edge (+$1.36/straddle guaranteed).
    // Bucket-based sizing: LowVol → $7/leg (about to become HighVol 90% of the time).
    if (elapsedPct >= 0.667 && elapsedPct < 0.95) {
      const preSize = (volBucket === 'LowVol') ? 7.0 : TRADE_SIZE;
      for (const preOutcome of ['UP', 'DOWN']) {
        signals.push({
          symbol,
          slug,   // current-window slug — executor strips timestamp, appends next window ts
          outcome:    preOutcome,
          side:       'BUY',
          size:       preSize,
          price:      PRE_ORDER_PRICE,
          confidence: 0.5,
          trigger:    'pre_order',
          reason:     `PRE-ORDER straddle (${preOutcome}) @ ${PRE_ORDER_PRICE} `
            + `— next window, ${(elapsedPct * 100).toFixed(0)}% elapsed. `
            + `Bucket=${volBucket}+${trendBucket}. $${preSize} USDC (${volBucket === 'LowVol' ? 'LowVol→HighVol sizing' : 'standard sizing'}).`,
        });
      }
    }

    // momentum_carry: previous window settled strongly → bet continuation early in new window
    // 61% BTC/ETH consecutive window continuation rate makes this +EV at entry <= 0.58.
    const { prevWindowOutcome } = m;
    if (
      prevWindowOutcome !== null &&
      elapsedPct >= 0.02 &&
      elapsedPct < 0.30
    ) {
      const mcPrice = parseFloat(
        (prevWindowOutcome === 'UP' ? m.upPrice : m.downPrice).toFixed(4));
      if (mcPrice <= 0.58) {
        signals.push({
          symbol,
          slug,
          outcome:    prevWindowOutcome,
          side:       'BUY',
          size:       TRADE_SIZE,
          price:      mcPrice,
          confidence: 0.61,
          trigger:    'momentum_carry',
          reason:     `MOMENTUM CARRY (${prevWindowOutcome}) — prev window settled strongly `
            + `at ${(elapsedPct * 100).toFixed(0)}% into new window. `
            + `Bucket=${volBucket}+${trendBucket} (RV60=${rv60}, Eff60=${eff60}). `
            + `Fixed $${TRADE_SIZE} USDC @ ${mcPrice.toFixed(4)}.`,
        });
      }
    }
  }

  return signals;
}

function updateSignalsJSON(signals, tsStr) {
  DATA_DIR && fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(DATA_DIR, 'signals.json'),
    JSON.stringify({ updated: tsStr, data: signals }, null, 2)
  );
}
// ─── README.md generation ─────────────────────────────────────────────────────
function generateReadme(results, tsStr, portfolioPath) {
  let portfolio = { balance: null, realized_pnl: null };
  try { portfolio = JSON.parse(fs.readFileSync(portfolioPath, 'utf8')); } catch {}
  const bal = portfolio.balance !== null
    ? `$${Number(portfolio.balance).toFixed(2)}` : 'N/A';
  const pnl = portfolio.realized_pnl !== null
    ? (portfolio.realized_pnl >= 0 ? '+' : '') + `$${Number(portfolio.realized_pnl).toFixed(4)}` : 'N/A';
  const rows = SYMBOLS
    .filter(s => results[s])
    .map(s => {
      const m = results[s];
      const bucket = `${m.volBucket}+${m.trendBucket}`;
      const elapsed = `${(m.elapsedPct * 100).toFixed(1)}%`;
      const remaining = `${Math.round(m.remainingSec)}s`;
      const dir60 = m.dir60pct !== null ? `${(m.dir60pct * 100).toFixed(1)}%` : '—';
      const dir80 = m.dir80pct !== null ? `${(m.dir80pct * 100).toFixed(1)}%` : '—';
      const dir90 = m.dir90pct !== null ? `${(m.dir90pct * 100).toFixed(1)}%` : '—';
      const arb = m.arbProfit ? `${(m.arbProfit * 100).toFixed(2)}%` : '—';
      return `| **${s}** | ${m.slug} | ${m.upPrice.toFixed(4)} | ${m.downPrice.toFixed(4)}`
           + ` | ${elapsed} | ${remaining} | ${bucket} | ${dir60} | ${dir80} | ${dir90} | ${arb} |`;
    })
    .join('\n');
  return `# Polymarket 15M Data Feed
> **Mode:** LIVE &nbsp;|&nbsp; **Updated:** \`${tsStr}\`
## Live Markets
| Symbol | Slug | UP | DOWN | Elapsed | Remaining | Bucket | Dir@60% | Dir@80% | Dir@90% | ARB |
| ------ | ----------------- | ------ | ------ | ------- | --------- | -------------- | ------- | ------- | ------- | ----- |
${rows}
## Portfolio
| Balance | Realized P&L |
| --------- | ------------- |
| ${bal} | ${pnl} |
## Reports
| Report | Description |
| ------------------------------ | ---------------------------------------- |
| [Data Collector](reports/data_collector.md) | Raw + calculated data log (last 48 h) |
| [Decision Summary](reports/decision_summary.md) | Analysis behind every signal (last 200) |
| [Decision Tracker](reports/decision_tracker.md) | Full trade history with entry, resolution and P&L |
| [Trigger Summary](reports/trigger_summary.md) | UP/DOWN trades, wins and losses by trigger (current epoch only) |
| [Trigger Summary v2](reports/trigger_summary_v2.md) | Trigger P&L by symbol — fresh epoch, clean baseline |
| [**V1.0 Prod** Trigger Summary](reports/trigger_summary_v1_Prod.md) | V1.0 Production — ring-fenced trigger performance |
| [**V2.0 Dev** Trigger Summary](reports/trigger_summary_v3.md) | V2.0 Dev — trend_follow + directional_90pct focus (forced suppressed) |
| [**V3.0 Dev** Trigger Summary](reports/trigger_summary_v4.md) | V3.0 Dev — all triggers active |
| [Market P&L](reports/market_pnl.md) | Bets and P&L per market window, grouped by symbol |
| [**V1.0 Prod** Market P&L](reports/market_V1_pnl.md) | V1.0 Production — market P&L view |
---
_Auto-generated by **Bob the builder**_
`;
}
// ─── data_collector.md generation ────────────────────────────────────────────
function generateDataCollectorReport(results, tsStr) {
  // Gather last 500 rows across all symbols from CSVs
  const allRows = [];
  for (const symbol of SYMBOLS) {
    const { rows } = loadCSV(symbol);
    for (const r of rows.slice(-200)) {
      const ts = r.ts || '';
      const d = new Date(ts);
      allRows.push({
        time:   isNaN(d) ? ts : formatETShort(d),
        symbol,
        slug:   r.slug || `${symbol.toLowerCase()}-updown-15m`,
        upPrice:    parseFloat(r.up_price || 0).toFixed(4),
        downPrice:  parseFloat(r.down_price || 0).toFixed(4),
        upSpread:   r.up_spread   || '—',
        dnSpread:   r.down_spread || '—',
        elapsed:    r.elapsed_pct !== undefined ? `${(parseFloat(r.elapsed_pct) * 100).toFixed(1)}%` : '—',
        volBucket:  r.vol_bucket   || '—',
        trend:      r.trend_bucket || '—',
        rv60:       r.rv60   || '—',
        eff60:      r.eff60  || '—',
        dirAt60:    r.dir_60pct !== undefined ? `${(parseFloat(r.dir_60pct) * 100).toFixed(1)}%` : '—',
        upTrades:   r.up_trade_count   || '0',
        dnTrades:   r.down_trade_count || '0',
        arb:        r.arb_profit || '—',
        sortTs:     d.getTime() || 0,
      });
    }
  }
  allRows.sort((a, b) => b.sortTs - a.sortTs);
  const top500 = allRows.slice(0, 500);
  const tableRows = top500.map(r =>
    `| \`${r.time}\` | ${r.symbol} | ${r.slug} | ${r.upPrice} | ${r.downPrice}`
    + ` | ${r.upSpread} | ${r.dnSpread} | ${r.elapsed} | ${r.volBucket}`
    + ` | ${r.trend} | ${r.rv60} | ${r.eff60} | ${r.dirAt60} | ${r.upTrades}`
    + ` | ${r.dnTrades} | ${r.arb} |`
  ).join('\n');
  return `# Data Collector Report
> **Updated:** \`${tsStr}\` &nbsp;|&nbsp; Last 500 market snapshots (48 h)
| Time (ET) | Symbol | Slug | UP Price | DOWN Price | UP Spread | DN Spread | Elapsed% | Vol Bucket | Trend | RV60 | Eff60 | P(UP)@60% | UP Trades | DN Trades | ARB |
| --------------- | ------ | ----------------- | -------- | -------- | -------- | -------- | -------- | --------- | ----- | ------- | ----- | --------- | --------- | --------- | ------ |
${tableRows}
---
_Auto-generated by **Bob the builder**_
`;
}
// ─── Stale-report timestamp bump ──────────────────────────────────────────────
function bumpReportTimestamp(filePath, tsStr) {
  if (!fs.existsSync(filePath)) return;
  let content = fs.readFileSync(filePath, 'utf8');
  // Replace the existing Updated: timestamp
  content = content.replace(
    /\*\*Updated:\*\*\s+`[^`]+`/,
    `**Updated:** \`${tsStr}\``
  );
  fs.writeFileSync(filePath, content);
}
// ─── Git commit & push ────────────────────────────────────────────────────────
function gitCommitAndPush(tsStr) {
  try {
    execSync('git config user.email "polymarket-feed@bot"', { cwd: REPO_DIR });
    execSync('git config user.name "Polymarket Feed"',      { cwd: REPO_DIR });
    execSync('git add -A', { cwd: REPO_DIR });
    // Check if there's anything to commit
    const status = execSync('git status --porcelain', { cwd: REPO_DIR }).toString().trim();
    if (!status) {
      console.log('  Nothing to commit.');
      return;
    }
    const isoTs = new Date().toISOString().substring(0, 16).replace('T', 'T') + ' ET';
    execSync(
      `git commit -m "data: ${new Date().toISOString().substring(0, 16)}:00 ET"`,
      { cwd: REPO_DIR }
    );
    // Push to origin (current branch)
    const branch = execSync('git rev-parse --abbrev-ref HEAD', { cwd: REPO_DIR })
      .toString().trim();
    execSync(`git push -u origin ${branch}`, { cwd: REPO_DIR });
    console.log(`  ✓ Pushed to origin/${branch}`);
  } catch (err) {
    console.error('  Git error:', err.message);
  }
}
// ─── Main ─────────────────────────────────────────────────────────────────────
function run() {
  console.log('='.repeat(60));
  const { results, tsStr, now } = collectMarketData();
  if (Object.keys(results).length === 0) {
    console.log('No markets fetched — skipping update.');
    return;
  }
  console.log('\nUpdating data files…');
  appendToCSVs(results, now);
  updateMarketsJSON(results, tsStr);
  // Generate and write trading signals
  const signals = generateSignals(results);
  updateSignalsJSON(signals, tsStr);
  const triggerCounts = signals.reduce((acc, s) => {
    acc[s.trigger] = (acc[s.trigger] || 0) + 1; return acc;
  }, {});
  console.log(`  signals: ${signals.length} generated — ${JSON.stringify(triggerCounts)}`);
  console.log('Generating reports…');
  fs.writeFileSync(path.join(REPO_DIR, 'README.md'),
    generateReadme(results, tsStr, path.join(DATA_DIR, 'portfolio.json')));
  fs.writeFileSync(path.join(REPORTS_DIR, 'data_collector.md'),
    generateDataCollectorReport(results, tsStr));
  // Bump timestamps on history-only reports (data unchanged, timestamp refreshed)
  for (const f of [
    'decision_summary.md', 'decision_tracker.md',
    'trigger_summary.md',  'trigger_summary_v1_Prod.md',
    'trigger_summary_v2.md', 'trigger_summary_v3.md', 'trigger_summary_v4.md',
    'market_pnl.md', 'market_V1_pnl.md',
  ]) {
    bumpReportTimestamp(path.join(REPORTS_DIR, f), tsStr);
  }
  console.log('Committing and pushing…');
  gitCommitAndPush(tsStr);
  console.log(`Done — ${tsStr}\n`);
}
// ─── Entry point ──────────────────────────────────────────────────────────────
const loopMode = process.argv.includes('--loop');
if (loopMode) {
  console.log(`Starting feed-updater in loop mode (interval: ${LOOP_INTERVAL / 1000}s)…`);
  run();
  setInterval(run, LOOP_INTERVAL);
} else {
  try { run(); } catch (err) { console.error('Fatal:', err); process.exit(1); }
}
