const sqlite3 = require('sqlite3').verbose();
const db = new sqlite3.Database('./market-data.db');

// Calculate log returns, realized volatility, and efficiency
async function calculateMetrics(marketId) {
  return new Promise((resolve, reject) => {
    // Get last 60 minutes of data (60 rows for 1-min intervals)
    const query = `
      SELECT timestamp, price 
      FROM market_data 
      WHERE market_id = ? 
      ORDER BY timestamp DESC 
      LIMIT 60
    `;

    db.all(query, [marketId], (err, rows) => {
      if (err) return reject(err);
      if (rows.length < 60) {
        console.log(`Not enough data for ${marketId} (need 60, got ${rows.length})`);
        return resolve(null);
      }

      // Reverse to get chronological order
      const prices = rows.reverse().map(r => r.price);
      const currentTimestamp = rows[rows.length - 1].timestamp;

      // 1. Calculate log returns
      const logReturns = [];
      for (let i = 1; i < prices.length; i++) {
        const r = Math.log(prices[i] / prices[i - 1]);
        logReturns.push(r);
      }

      // 2. Realized volatility (RV_60)
      const sumSquaredReturns = logReturns.reduce((sum, r) => sum + r * r, 0);
      const realizedVol60 = Math.sqrt(sumSquaredReturns);

      // 3. Efficiency (directional measure)
      const priceNow = prices[prices.length - 1];
      const price60mAgo = prices[0];
      const netMove = Math.abs(priceNow - price60mAgo);

      let totalAbsMove = 0;
      for (let i = 1; i < prices.length; i++) {
        totalAbsMove += Math.abs(prices[i] - prices[i - 1]);
      }

      const efficiency60 = totalAbsMove > 0 ? netMove / totalAbsMove : 0;

      // 4. Most recent log return
      const latestLogReturn = logReturns[logReturns.length - 1];

      resolve({
        timestamp: currentTimestamp,
        marketId,
        logReturn: latestLogReturn,
        realizedVol60,
        efficiency60,
      });
    });
  });
}

// Calculate median for bucketing
async function getMedians() {
  return new Promise((resolve, reject) => {
    db.all(`
      SELECT realized_vol_60m, efficiency_60m 
      FROM calculations 
      WHERE realized_vol_60m IS NOT NULL 
        AND efficiency_60m IS NOT NULL
      ORDER BY id DESC 
      LIMIT 1000
    `, (err, rows) => {
      if (err) return reject(err);
      if (rows.length < 10) {
        // Not enough historical data, use defaults
        return resolve({ volMedian: 0.02, effMedian: 0.5 });
      }

      const vols = rows.map(r => r.realized_vol_60m).sort((a, b) => a - b);
      const effs = rows.map(r => r.efficiency_60m).sort((a, b) => a - b);

      const volMedian = vols[Math.floor(vols.length / 2)];
      const effMedian = effs[Math.floor(effs.length / 2)];

      resolve({ volMedian, effMedian });
    });
  });
}

// Assign buckets
function assignBuckets(metrics, medians) {
  const volBucket = metrics.realizedVol60 <= medians.volMedian ? 'LowVol' : 'HighVol';
  const trendBucket = metrics.efficiency60 <= medians.effMedian ? 'Range' : 'Trend';

  return { volBucket, trendBucket };
}

// Main calculation loop
async function runCalculations() {
  try {
    // Get unique markets
    const markets = await new Promise((resolve, reject) => {
      db.all(`
        SELECT DISTINCT market_id 
        FROM market_data
      `, (err, rows) => {
        if (err) reject(err);
        else resolve(rows.map(r => r.market_id));
      });
    });

    if (markets.length === 0) {
      console.log('No markets found in database');
      return;
    }

    const medians = await getMedians();
    console.log(`Medians - Vol: ${medians.volMedian.toFixed(4)}, Eff: ${medians.effMedian.toFixed(4)}`);

    for (const marketId of markets) {
      const metrics = await calculateMetrics(marketId);
      if (!metrics) continue;

      const buckets = assignBuckets(metrics, medians);

      // Insert into calculations table
      db.run(`
        INSERT INTO calculations (
          timestamp, market_id, interval_minutes, log_return, 
          realized_vol_60m, efficiency_60m, vol_bucket, trend_bucket
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      `, [
        metrics.timestamp,
        metrics.marketId,
        12, // We're calculating at minute 12
        metrics.logReturn,
        metrics.realizedVol60,
        metrics.efficiency60,
        buckets.volBucket,
        buckets.trendBucket
      ], (err) => {
        if (err) console.error('DB insert error:', err.message);
      });

      console.log(`[${new Date().toISOString()}] ${marketId} | Vol: ${metrics.realizedVol60.toFixed(4)} (${buckets.volBucket}) | Eff: ${metrics.efficiency60.toFixed(3)} (${buckets.trendBucket})`);
    }

  } catch (err) {
    console.error('Error in runCalculations:', err.message);
  }
}

// Run every 1 minute (but calculations require 60 minutes of data)
console.log('Starting calculation engine...');
runCalculations();
setInterval(runCalculations, 60 * 1000);
