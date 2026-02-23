const sqlite3 = require('sqlite3').verbose();
require('dotenv').config();

// Database setup
const db = new sqlite3.Database('./market-data.db');

// Create tables (same as before)
db.serialize(() => {
  db.run(`
    CREATE TABLE IF NOT EXISTS market_data (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp INTEGER NOT NULL,
      market_id TEXT NOT NULL,
      market_name TEXT,
      token_id TEXT NOT NULL,
      outcome TEXT,
      price REAL NOT NULL,
      bid REAL,
      ask REAL,
      spread REAL,
      volume_24h REAL,
      bid_depth REAL,
      ask_depth REAL,
      created_at INTEGER DEFAULT (strftime('%s', 'now'))
    )
  `);

  db.run(`
    CREATE INDEX IF NOT EXISTS idx_market_timestamp 
    ON market_data(market_id, timestamp)
  `);

  db.run(`
    CREATE TABLE IF NOT EXISTS calculations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp INTEGER NOT NULL,
      market_id TEXT NOT NULL,
      interval_minutes INTEGER NOT NULL,
      log_return REAL,
      realized_vol_60m REAL,
      efficiency_60m REAL,
      vol_bucket TEXT,
      trend_bucket TEXT,
      created_at INTEGER DEFAULT (strftime('%s', 'now'))
    )
  `);
});

async function getActive15MinSlugs() {
  try {
    // Scrape the /crypto/15M page to get current market slugs
    const response = await fetch('https://polymarket.com/crypto/15M');
    const html = await response.text();
    
    // Extract slugs from HTML
    const slugMatches = html.match(/event\/([a-z0-9-]+15m-\d+)/g);
    if (!slugMatches) return [];
    
    const slugs = [...new Set(slugMatches)].map(s => s.replace('event/', ''));
    return slugs;
  } catch (err) {
    console.error('Error scraping /crypto/15M:', err.message);
    return [];
  }
}

async function fetchMarketBySlug(slug) {
  try {
    const response = await fetch(`https://gamma-api.polymarket.com/markets?slug=${slug}`);
    const markets = await response.json();
    
    if (markets.length === 0) return null;
    
    const market = markets[0];
    
    // Parse JSON strings
    const tokenIds = JSON.parse(market.clobTokenIds || '[]');
    const outcomes = JSON.parse(market.outcomes || '[]');
    const prices = JSON.parse(market.outcomePrices || '[]');
    
    return {
      slug: market.slug,
      conditionId: market.conditionId,
      question: market.question,
      closed: market.closed,
      active: market.active,
      tokenIds,
      outcomes,
      prices,
      volume: parseFloat(market.volumeNum || 0),
      liquidity: parseFloat(market.liquidityNum || 0),
    };
  } catch (err) {
    console.error(`Error fetching market ${slug}:`, err.message);
    return null;
  }
}

async function fetchOrderBook(tokenId) {
  try {
    const response = await fetch(`https://clob.polymarket.com/book?token_id=${tokenId}`);
    const orderBook = await response.json();
    
    const bids = orderBook.bids || [];
    const asks = orderBook.asks || [];
    
    const bestBid = bids.length > 0 ? parseFloat(bids[0].price) : null;
    const bestAsk = asks.length > 0 ? parseFloat(asks[0].price) : null;
    const spread = (bestBid && bestAsk) ? bestAsk - bestBid : null;
    
    const bidDepth = bids.slice(0, 5).reduce((sum, b) => sum + parseFloat(b.size || 0), 0);
    const askDepth = asks.slice(0, 5).reduce((sum, a) => sum + parseFloat(a.size || 0), 0);
    
    return {
      bestBid,
      bestAsk,
      spread,
      bidDepth,
      askDepth,
    };
  } catch (err) {
    console.error(`Error fetching order book for ${tokenId}:`, err.message);
    return null;
  }
}

async function collectData() {
  try {
    const slugs = await getActive15MinSlugs();
    
    if (slugs.length === 0) {
      console.log(`[${new Date().toISOString()}] No active 15-minute markets found`);
      return;
    }
    
    console.log(`[${new Date().toISOString()}] Found ${slugs.length} active markets`);
    
    const timestamp = Math.floor(Date.now() / 1000);
    
    for (const slug of slugs) {
      const market = await fetchMarketBySlug(slug);
      if (!market || market.closed) continue;
      
      // Fetch data for each token (Up and Down)
      for (let i = 0; i < market.tokenIds.length; i++) {
        const tokenId = market.tokenIds[i];
        const outcome = market.outcomes[i];
        const price = parseFloat(market.prices[i]);
        
        const orderBook = await fetchOrderBook(tokenId);
        if (!orderBook) continue;
        
        const midPrice = (orderBook.bestBid && orderBook.bestAsk) 
          ? (orderBook.bestBid + orderBook.bestAsk) / 2 
          : price;
        
        // Insert into database
        db.run(`
          INSERT INTO market_data (
            timestamp, market_id, market_name, token_id, outcome,
            price, bid, ask, spread, volume_24h, bid_depth, ask_depth
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        `, [
          timestamp,
          market.conditionId,
          market.question,
          tokenId,
          outcome,
          midPrice,
          orderBook.bestBid,
          orderBook.bestAsk,
          orderBook.spread,
          market.volume,
          orderBook.bidDepth,
          orderBook.askDepth,
        ], (err) => {
          if (err) console.error('DB insert error:', err.message);
        });
        
        console.log(`[${new Date().toISOString()}] ${market.question.slice(0, 40)}... | ${outcome} | Price: ${midPrice.toFixed(4)} | Spread: ${(orderBook.spread || 0).toFixed(4)}`);
        
        // Rate limit
        await new Promise(r => setTimeout(r, 200));
      }
    }
    
  } catch (err) {
    console.error('Error in collectData:', err.message);
  }
}

// Run immediately, then every 1 minute
console.log('Starting market data collector (v3 - scraper + API)...');
console.log('Database: market-data.db');
console.log('Scraping /crypto/15M page for active markets...');
collectData();
setInterval(collectData, 60 * 1000);
