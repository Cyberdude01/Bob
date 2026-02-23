const sqlite3 = require('sqlite3').verbose();
const db = new sqlite3.Database('./market-data.db');

// Threshold values to test (in decimal, e.g., 0.0008 = 0.08%)
const X_VALUES = [0.0008, 0.0012, 0.0020]; // 0.08%, 0.12%, 0.20%

// Analyze 15-minute candles
async function analyze15MinCandles(marketId) {
  return new Promise((resolve, reject) => {
    // Get all data for this market, ordered by time
    db.all(`
      SELECT timestamp, price 
      FROM market_data 
      WHERE market_id = ? 
      ORDER BY timestamp ASC
    `, [marketId], async (err, rows) => {
      if (err) return reject(err);
      
      const candles = [];
      
      // Group into 15-minute candles
      let candleStart = Math.floor(rows[0].timestamp / 900) * 900; // Round to 15-min boundary
      
      for (let i = 0; i < rows.length; i++) {
        const currentCandleStart = Math.floor(rows[i].timestamp / 900) * 900;
        
        if (currentCandleStart !== candleStart) {
          candleStart = currentCandleStart;
        }
        
        const minuteInCandle = Math.floor((rows[i].timestamp - candleStart) / 60);
        
        if (minuteInCandle === 12) {
          // This is the 12th minute - we need to analyze from here
          const P12 = rows[i].price;
          
          // Find the close (15th minute)
          let closePriceRow = null;
          for (let j = i; j < rows.length && j < i + 3; j++) {
            const minInCandleJ = Math.floor((rows[j].timestamp - candleStart) / 60);
            if (minInCandleJ >= 14) { // 15th minute (0-indexed is 14)
              closePriceRow = rows[j];
              break;
            }
          }
          
          if (!closePriceRow) continue; // No close data
          
          const closePrice = closePriceRow.price;
          const R_rem = Math.log(closePrice / P12); // Log return for last 3 minutes
          
          // Get the bucket at minute 12
          const bucketData = await getBucketAtTime(marketId, rows[i].timestamp);
          
          if (bucketData) {
            candles.push({
              candleStart,
              P12,
              closePrice,
              R_rem,
              volBucket: bucketData.volBucket,
              trendBucket: bucketData.trendBucket,
            });
          }
        }
      }
      
      resolve(candles);
    });
  });
}

// Get bucket classification at a specific timestamp
async function getBucketAtTime(marketId, timestamp) {
  return new Promise((resolve, reject) => {
    db.get(`
      SELECT vol_bucket, trend_bucket 
      FROM calculations 
      WHERE market_id = ? 
        AND timestamp <= ? 
      ORDER BY timestamp DESC 
      LIMIT 1
    `, [marketId, timestamp], (err, row) => {
      if (err) reject(err);
      else resolve(row);
    });
  });
}

// Build probability table
async function buildProbabilityTable() {
  try {
    // Get all markets
    const markets = await new Promise((resolve, reject) => {
      db.all(`SELECT DISTINCT market_id FROM market_data`, (err, rows) => {
        if (err) reject(err);
        else resolve(rows.map(r => r.market_id));
      });
    });

    // Collect all candles across all markets
    let allCandles = [];
    for (const marketId of markets) {
      const candles = await analyze15MinCandles(marketId);
      allCandles = allCandles.concat(candles);
    }

    console.log(`\nAnalyzed ${allCandles.length} 15-minute candles`);
    
    if (allCandles.length === 0) {
      console.log('No candles to analyze yet. Need more data.');
      return;
    }

    // Group by buckets
    const buckets = {
      'LowVol+Range': [],
      'LowVol+Trend': [],
      'HighVol+Range': [],
      'HighVol+Trend': [],
    };

    for (const candle of allCandles) {
      const key = `${candle.volBucket}+${candle.trendBucket}`;
      if (buckets[key]) {
        buckets[key].push(candle);
      }
    }

    console.log('\n=== PROBABILITY TABLE ===\n');
    console.log('Bucket              | Count | X=0.08%  | X=0.12%  | X=0.20%');
    console.log('--------------------+-------+----------+----------+---------');

    for (const [bucketName, candles] of Object.entries(buckets)) {
      if (candles.length === 0) {
        console.log(`${bucketName.padEnd(19)} |     0 | N/A      | N/A      | N/A`);
        continue;
      }

      const probs = X_VALUES.map(X => {
        const count = candles.filter(c => Math.abs(c.R_rem) <= X).length;
        const prob = count / candles.length;
        return (prob * 100).toFixed(1) + '%';
      });

      console.log(`${bucketName.padEnd(19)} | ${candles.length.toString().padStart(5)} | ${probs[0].padStart(8)} | ${probs[1].padStart(8)} | ${probs[2].padStart(8)}`);
    }

    console.log('\n=== BUCKET DISTRIBUTION ===\n');
    for (const [bucketName, candles] of Object.entries(buckets)) {
      console.log(`${bucketName}: ${candles.length} candles`);
    }

    // Save to file
    const fs = require('fs');
    fs.writeFileSync('./probability-table.json', JSON.stringify({
      generatedAt: new Date().toISOString(),
      totalCandles: allCandles.length,
      xValues: X_VALUES,
      buckets: Object.fromEntries(
        Object.entries(buckets).map(([name, candles]) => [
          name,
          {
            count: candles.length,
            probabilities: X_VALUES.map(X => ({
              X,
              probability: candles.filter(c => Math.abs(c.R_rem) <= X).length / candles.length,
            })),
          },
        ])
      ),
    }, null, 2));

    console.log('\n✅ Probability table saved to probability-table.json');

  } catch (err) {
    console.error('Error building probability table:', err.message);
  }
}

// Run analysis
console.log('Building probability table from historical data...');
buildProbabilityTable().then(() => {
  console.log('\nDone!');
  db.close();
});
