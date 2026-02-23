/**
 * Polymarket Spread Analyzer
 * Analyzes order books for arbitrage opportunities
 */

const axios = require('axios');

const GAMMA_API = 'https://gamma-api.polymarket.com';
const CLOB_API = 'https://clob.polymarket.com';

async function getOrderBook(tokenId) {
  try {
    const res = await axios.get(`${CLOB_API}/book?token_id=${tokenId}`);
    return res.data;
  } catch (e) {
    return { bids: [], asks: [] };
  }
}

function analyzeLiquidity(book, side) {
  const orders = side === 'bid' ? book.bids : book.asks;
  if (!orders?.length) return null;
  
  const best = parseFloat(orders[0].price);
  const totalSize = orders.reduce((sum, o) => sum + parseFloat(o.size), 0);
  const totalValue = orders.reduce((sum, o) => sum + parseFloat(o.size) * parseFloat(o.price), 0);
  
  // Liquidity at different price levels
  const within1c = orders.filter(o => Math.abs(parseFloat(o.price) - best) <= 0.01)
    .reduce((sum, o) => sum + parseFloat(o.size), 0);
  const within5c = orders.filter(o => Math.abs(parseFloat(o.price) - best) <= 0.05)
    .reduce((sum, o) => sum + parseFloat(o.size), 0);
    
  return { best, totalSize, totalValue, within1c, within5c, count: orders.length };
}

async function analyzeMarket(market) {
  const tokenIds = JSON.parse(market.clobTokenIds || '[]');
  if (tokenIds.length < 2) return null;
  
  const [bookUp, bookDown] = await Promise.all([
    getOrderBook(tokenIds[0]),
    getOrderBook(tokenIds[1])
  ]);
  
  const upBids = analyzeLiquidity(bookUp, 'bid');
  const upAsks = analyzeLiquidity(bookUp, 'ask');
  const downBids = analyzeLiquidity(bookDown, 'bid');
  const downAsks = analyzeLiquidity(bookDown, 'ask');
  
  if (!upAsks || !downAsks) return null;
  
  // Key metrics
  const bestAskUp = upAsks.best;
  const bestAskDown = downAsks.best;
  const combinedCost = bestAskUp + bestAskDown;
  const spread = 1 - combinedCost;  // Profit if both bought and one wins
  
  const bestBidUp = upBids?.best || 0;
  const bestBidDown = downBids?.best || 0;
  const midUp = (bestAskUp + bestBidUp) / 2;
  const midDown = (bestAskDown + bestBidDown) / 2;
  
  return {
    title: market.question,
    endDate: market.endDate,
    up: {
      bestBid: bestBidUp,
      bestAsk: bestAskUp,
      spread: bestAskUp - bestBidUp,
      mid: midUp,
      askLiquidity: upAsks.within5c,
      bidLiquidity: upBids?.within5c || 0,
      depth: { bids: bookUp.bids?.length || 0, asks: bookUp.asks?.length || 0 }
    },
    down: {
      bestBid: bestBidDown,
      bestAsk: bestAskDown,
      spread: bestAskDown - bestBidDown,
      mid: midDown,
      askLiquidity: downAsks.within5c,
      bidLiquidity: downBids?.within5c || 0,
      depth: { bids: bookDown.bids?.length || 0, asks: bookDown.asks?.length || 0 }
    },
    combined: {
      costToBuyBoth: combinedCost,
      guaranteedProfit: spread,
      profitPercent: (spread / combinedCost * 100).toFixed(2)
    }
  };
}

async function main() {
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('           POLYMARKET SPREAD ANALYZER');
  console.log('           ' + new Date().toISOString().slice(0, 19));
  console.log('═══════════════════════════════════════════════════════════════════\n');

  // Find active crypto Up/Down markets
  const res = await axios.get(`${GAMMA_API}/markets?active=true&closed=false&limit=100`);
  const cryptoMarkets = res.data.filter(m => 
    m.question?.includes('Up or Down') && 
    (m.question?.includes('Ethereum') || m.question?.includes('Bitcoin'))
  );
  
  console.log(`Found ${cryptoMarkets.length} active crypto Up/Down markets\n`);
  
  if (cryptoMarkets.length === 0) {
    // Fall back to any binary market
    console.log('No crypto markets active. Checking other markets...\n');
    const otherMarkets = res.data.filter(m => m.clobTokenIds?.length >= 2).slice(0, 5);
    
    for (const market of otherMarkets) {
      const analysis = await analyzeMarket(market);
      if (analysis) printAnalysis(analysis);
    }
    return;
  }
  
  // Analyze each market
  const opportunities = [];
  
  for (const market of cryptoMarkets.slice(0, 10)) {
    const analysis = await analyzeMarket(market);
    if (analysis) {
      opportunities.push(analysis);
      printAnalysis(analysis);
    }
    await new Promise(r => setTimeout(r, 200)); // Rate limit
  }
  
  // Summary
  console.log('\n═══════════════════════════════════════════════════════════════════');
  console.log('                        OPPORTUNITIES');
  console.log('═══════════════════════════════════════════════════════════════════\n');
  
  const profitable = opportunities.filter(o => o.combined.guaranteedProfit > 0);
  
  if (profitable.length === 0) {
    console.log('❌ No guaranteed arbitrage opportunities found');
    console.log('   (Combined ask prices > $1.00 on all markets)\n');
    
    // Show best near-opportunities
    const sorted = opportunities.sort((a, b) => b.combined.guaranteedProfit - a.combined.guaranteedProfit);
    console.log('📊 Best spreads (closest to profitable):');
    sorted.slice(0, 3).forEach(o => {
      console.log(`   ${o.title.slice(0, 50)}...`);
      console.log(`   Cost: $${o.combined.costToBuyBoth.toFixed(4)} | Gap: $${Math.abs(o.combined.guaranteedProfit).toFixed(4)}`);
      console.log('');
    });
  } else {
    console.log(`✅ Found ${profitable.length} arbitrage opportunities!\n`);
    profitable.forEach(o => {
      console.log(`🎯 ${o.title.slice(0, 50)}...`);
      console.log(`   Buy Up @ $${o.up.bestAsk.toFixed(3)} + Down @ $${o.down.bestAsk.toFixed(3)} = $${o.combined.costToBuyBoth.toFixed(4)}`);
      console.log(`   PROFIT: $${o.combined.guaranteedProfit.toFixed(4)} per share (${o.combined.profitPercent}%)`);
      console.log(`   Liquidity: Up ${o.up.askLiquidity.toFixed(0)} / Down ${o.down.askLiquidity.toFixed(0)} shares`);
      console.log('');
    });
  }
}

function printAnalysis(a) {
  console.log(`📈 ${a.title.slice(0, 60)}${a.title.length > 60 ? '...' : ''}`);
  console.log(`   Ends: ${a.endDate}`);
  console.log('');
  console.log('   ┌─────────────┬───────────┬───────────┬───────────┬───────────┐');
  console.log('   │   Side      │  Best Bid │  Best Ask │   Spread  │ Liquidity │');
  console.log('   ├─────────────┼───────────┼───────────┼───────────┼───────────┤');
  console.log(`   │ Up/Yes      │   $${a.up.bestBid.toFixed(3).padStart(5)}  │   $${a.up.bestAsk.toFixed(3).padStart(5)}  │   $${a.up.spread.toFixed(3).padStart(5)}  │  ${a.up.askLiquidity.toFixed(0).padStart(7)}  │`);
  console.log(`   │ Down/No     │   $${a.down.bestBid.toFixed(3).padStart(5)}  │   $${a.down.bestAsk.toFixed(3).padStart(5)}  │   $${a.down.spread.toFixed(3).padStart(5)}  │  ${a.down.askLiquidity.toFixed(0).padStart(7)}  │`);
  console.log('   └─────────────┴───────────┴───────────┴───────────┴───────────┘');
  console.log(`   Combined cost: $${a.combined.costToBuyBoth.toFixed(4)} | ${a.combined.guaranteedProfit >= 0 ? '✅' : '❌'} Profit: $${a.combined.guaranteedProfit.toFixed(4)} (${a.combined.profitPercent}%)`);
  console.log(`   Depth: Up [${a.up.depth.bids}b/${a.up.depth.asks}a] Down [${a.down.depth.bids}b/${a.down.depth.asks}a]`);
  console.log('');
}

main().catch(e => console.error('Error:', e.message));
