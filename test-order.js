const { ClobClient } = require('@polymarket/clob-client');
const { ethers } = require('ethers');
const axios = require('axios');
require('dotenv').config();

async function testOrder() {
  console.log('🇨🇦 Testing from Toronto, Canada\n');
  
  // Check IP
  try {
    const ipRes = await axios.get('https://ipinfo.io/json');
    console.log('IP:', ipRes.data.ip, '|', ipRes.data.city + ',', ipRes.data.country);
  } catch {}
  
  // Initialize client
  console.log('\nInitializing client...');
  const wallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY);
  console.log('Wallet:', wallet.address);
  
  const tempClient = new ClobClient('https://clob.polymarket.com', 137, wallet);
  const creds = await tempClient.deriveApiKey();
  const client = new ClobClient('https://clob.polymarket.com', 137, wallet, creds);
  console.log('✅ Authenticated');
  
  // Get current ETH 15m market
  const now = Math.floor(Date.now() / 1000);
  const windowStart = Math.floor(now / 900) * 900;
  const slug = 'eth-updown-15m-' + windowStart;
  
  console.log('\nFetching market:', slug);
  const mktRes = await axios.get('https://gamma-api.polymarket.com/markets?slug=' + slug);
  const market = Array.isArray(mktRes.data) ? mktRes.data[0] : mktRes.data;
  
  if (!market) {
    console.log('No market found');
    return;
  }
  
  console.log('Market:', market.question);
  const prices = JSON.parse(market.outcomePrices || '[]');
  console.log('Prices → Up:', prices[0], '| Down:', prices[1]);
  
  const tokenIds = JSON.parse(market.clobTokenIds || '[]');
  
  console.log('\n📝 Placing order: BUY Up @ 0.48 for $1...');
  
  try {
    const order = await client.createAndPostOrder({
      tokenID: tokenIds[0],
      side: 'BUY',
      price: 0.48,
      size: 1,
    });
    
    console.log('\n════════════════════════════════════════════════════');
    console.log('🎉 ORDER PLACED SUCCESSFULLY!');
    console.log('════════════════════════════════════════════════════');
    console.log(JSON.stringify(order, null, 2));
  } catch (e) {
    console.log('\n❌ Order Error:', e.response?.data?.error || e.message);
    if (e.response?.status) console.log('   Status:', e.response.status);
  }
}

testOrder().catch(e => console.error('Fatal:', e.message));
