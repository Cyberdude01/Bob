const sqlite3 = require('sqlite3').verbose();
const db = new sqlite3.Database('./market-data.db');

db.serialize(() => {
  db.run(`DROP TABLE IF EXISTS market_data`);
  db.run(`DROP TABLE IF EXISTS calculations`);
  
  db.run(`
    CREATE TABLE market_data (
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
      created_at INTEGER
    )
  `);
  
  db.run(`CREATE INDEX idx_market_timestamp ON market_data(market_id, timestamp)`);
  
  db.run(`
    CREATE TABLE calculations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp INTEGER NOT NULL,
      market_id TEXT NOT NULL,
      interval_minutes INTEGER NOT NULL,
      log_return REAL,
      realized_vol_60m REAL,
      efficiency_60m REAL,
      vol_bucket TEXT,
      trend_bucket TEXT,
      created_at INTEGER
    )
  `, (err) => {
    if (err) {
      console.error('Error:', err.message);
    } else {
      console.log('✅ Database schema created successfully');
    }
    db.close();
  });
});
