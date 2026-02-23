const fs = require('fs');
const path = require('path');
const axios = require('axios');

const LOG_FILE = '/root/polymarket-bot/trades.json';

// Initialize log file if doesn't exist
function initLog() {
  if (!fs.existsSync(LOG_FILE)) {
    fs.writeFileSync(LOG_FILE, JSON.stringify({ trades: [], summary: { totalCost: 0, totalPayout: 0, wins: 0, losses: 0, pending: 0 } }, null, 2));
  }
  return JSON.parse(fs.readFileSync(LOG_FILE, 'utf8'));
}

// Log a new trade
function logTrade(trade) {
  const data = initLog();
  
  const entry = {
    id: Date.now().toString(),
    timestamp: new Date().toISOString(),
    marketSlug: trade.slug || 'unknown',
    tokenId: trade.tokenId,
    side: trade.side,
    outcome: trade.outcome, // 'Up' or 'Down'
    size: trade.size,
    price: trade.price,
    cost: trade.size * trade.price,
    orderId: trade.orderId || null,
    status: 'PENDING',
    payout: 0,
    profit: 0,
    resolvedAt: null,
    marketResult: null
  };
  
  data.trades.push(entry);
  data.summary.totalCost += entry.cost;
  data.summary.pending++;
  
  fs.writeFileSync(LOG_FILE, JSON.stringify(data, null, 2));
  
  console.log('📝 Trade logged:', entry.outcome, entry.size, '@ $' + entry.price, '= $' + entry.cost.toFixed(2));
  return entry;
}

// Check and update pending trades
async function checkPendingTrades() {
  const data = initLog();
  let updated = 0;
  
  for (const trade of data.trades) {
    if (trade.status !== 'PENDING') continue;
    
    // Calculate the market end time (15 min after window start)
    const windowStart = parseInt(trade.marketSlug?.split('-').pop() || 0);
    const windowEnd = windowStart + 900;
    const now = Math.floor(Date.now() / 1000);
    
    // Only check if market should be resolved (5 min buffer)
    if (now < windowEnd + 300) continue;
    
    try {
      const res = await axios.get('https://gamma-api.polymarket.com/markets?slug=' + trade.marketSlug, { timeout: 10000 });
      const market = Array.isArray(res.data) ? res.data[0] : res.data;
      
      if (market && (market.resolved || market.outcome)) {
        const winner = market.outcome || market.winner;
        const won = (trade.outcome === 'Up' && winner === 'Up') || 
                    (trade.outcome === 'Down' && winner === 'Down');
        
        trade.status = won ? 'WIN' : 'LOSS';
        trade.marketResult = winner;
        trade.payout = won ? trade.size : 0;
        trade.profit = trade.payout - trade.cost;
        trade.resolvedAt = new Date().toISOString();
        
        data.summary.pending--;
        if (won) {
          data.summary.wins++;
          data.summary.totalPayout += trade.payout;
        } else {
          data.summary.losses++;
        }
        
        updated++;
        console.log((won ? '✅' : '❌'), trade.outcome, '@ $' + trade.price, '->', winner, '|', won ? '+$' + trade.profit.toFixed(2) : '-$' + trade.cost.toFixed(2));
      }
    } catch (e) {
      // Market not found - might be too old
    }
    
    await new Promise(r => setTimeout(r, 200));
  }
  
  if (updated > 0) {
    fs.writeFileSync(LOG_FILE, JSON.stringify(data, null, 2));
  }
  
  return updated;
}

// Get summary
function getSummary() {
  const data = initLog();
  const s = data.summary;
  const pnl = s.totalPayout - s.totalCost;
  const winRate = s.wins + s.losses > 0 ? (s.wins / (s.wins + s.losses) * 100) : 0;
  
  return {
    totalTrades: data.trades.length,
    wins: s.wins,
    losses: s.losses,
    pending: s.pending,
    winRate: winRate.toFixed(1) + '%',
    totalCost: '$' + s.totalCost.toFixed(2),
    totalPayout: '$' + s.totalPayout.toFixed(2),
    netPnL: (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2),
    roi: s.totalCost > 0 ? ((pnl / s.totalCost) * 100).toFixed(1) + '%' : '0%'
  };
}

// Print report
function printReport() {
  const data = initLog();
  const s = getSummary();
  
  console.log('');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log('                    TRADE LOG REPORT');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log('');
  console.log('📊 SUMMARY');
  console.log('   Total trades:', s.totalTrades);
  console.log('   Wins:', s.wins, '| Losses:', s.losses, '| Pending:', s.pending);
  console.log('   Win rate:', s.winRate);
  console.log('');
  console.log('💰 P&L');
  console.log('   Total cost:', s.totalCost);
  console.log('   Total payout:', s.totalPayout);
  console.log('   Net P&L:', s.netPnL);
  console.log('   ROI:', s.roi);
  console.log('');
  
  if (data.trades.length > 0) {
    console.log('📋 RECENT TRADES (last 10)');
    console.log('─────────────────────────────────────────────────────────────');
    data.trades.slice(-10).reverse().forEach(t => {
      const emoji = t.status === 'WIN' ? '✅' : t.status === 'LOSS' ? '❌' : '⏳';
      const date = t.timestamp.slice(5, 16).replace('T', ' ');
      console.log(emoji, date, t.outcome.padEnd(4), t.size.toFixed(2).padStart(5), '@ $' + t.price.toFixed(2), '=', ('$' + t.cost.toFixed(2)).padStart(6), '|', t.status);
    });
  }
  
  console.log('');
  console.log('═══════════════════════════════════════════════════════════════');
}

module.exports = { logTrade, checkPendingTrades, getSummary, printReport, initLog };

// CLI
if (require.main === module) {
  const cmd = process.argv[2];
  
  if (cmd === 'report') {
    printReport();
  } else if (cmd === 'check') {
    console.log('Checking pending trades...');
    checkPendingTrades().then(n => console.log('Updated', n, 'trades'));
  } else if (cmd === 'summary') {
    console.log(getSummary());
  } else {
    console.log('Usage: node trade-logger.js [report|check|summary]');
  }
}
