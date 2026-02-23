const { ClobClient } = require('@polymarket/clob-client');
const { ethers } = require('ethers');
const axios = require('axios');
const { logTrade, printReport } = require('./trade-logger');
require('dotenv').config();

async function placeTrade(outcome, price, size) {
  const wallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY);
  const proxyAddress = '0x0A9551d6C30c6E8C3d2948c8386077f246cece95';
  
  const tempClient = new ClobClient('https://clob.polymarket.com', 137, wallet);
  const creds = await tempClient.deriveApiKey();
  const client = new ClobClient('https://clob.polymarket.com', 137, wallet, creds, 2, proxyAddress);
  
  // Get current ETH 15m market
  const now = Math.floor(Date.now() / 1000);
  const windowStart = Math.floor(now / 900) * 900;
  const slug = 'eth-updown-15m-' + windowStart;
  
  console.log('🎯 Market:', slug);
  
  const mktRes = await axios.get('https://gamma-api.polymarket.com/markets?slug=' + slug);
  const market = Array.isArray(mktRes.data) ? mktRes.data[0] : mktRes.data;
  
  if (!market) {
    console.log('❌ Market not found');
    return;
  }
  
  console.log('📊', market.question);
  const prices = JSON.parse(market.outcomePrices || '[]');
  console.log('   Current: Up', prices[0], '| Down', prices[1]);
  
  const tokenIds = JSON.parse(market.clobTokenIds || '[]');
  const tokenId = outcome.toLowerCase() === 'up' ? tokenIds[0] : tokenIds[1];
  
  console.log('');
  console.log('📝 Placing order: BUY', outcome, size, '@ $' + price);
  
  try {
    const order = await client.createAndPostOrder({
      tokenID: tokenId,
      side: 'BUY',
      price: parseFloat(price),
      size: parseInt(size),
    });
    
    if (order.success) {
      console.log('✅ Order placed! ID:', order.orderID?.slice(0, 20) + '...');
      
      // Log the trade
      logTrade({
        slug: slug,
        tokenId: tokenId,
        side: 'BUY',
        outcome: outcome,
        size: parseInt(size),
        price: parseFloat(price),
        orderId: order.orderID
      });
      
    } else {
      console.log('❌ Order failed:', order.error);
    }
  } catch (e) {
    console.log('❌ Error:', e.response?.data?.error || e.message);
  }
}

// CLI: node place-trade.js <up|down> <price> <size>
if (require.main === module) {
  const [outcome, price, size] = process.argv.slice(2);
  
  if (!outcome || !price || !size) {
    console.log('Usage: node place-trade.js <up|down> <price> <size>');
    console.log('Example: node place-trade.js up 0.48 5');
    process.exit(1);
  }
  
  placeTrade(outcome, price, size).catch(e => console.error('Fatal:', e.message));
}

module.exports = { placeTrade };
