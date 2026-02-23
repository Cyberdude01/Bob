const { ClobClient } = require('@polymarket/clob-client');
const { ethers } = require('ethers');
const axios = require('axios');
const { logTrade } = require('./trade-logger');
require('dotenv').config();

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;

async function sendTelegramNotification(message) {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
    console.log('Telegram not configured, skipping notification');
    return;
  }
  
  try {
    await axios.post(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
      chat_id: TELEGRAM_CHAT_ID,
      text: message,
      parse_mode: 'HTML'
    });
    console.log('📱 Telegram notification sent');
  } catch (e) {
    console.log('Failed to send Telegram:', e.message);
  }
}

async function placeAutoTrades() {
  const startTime = new Date();
  console.log('');
  console.log('════════════════════════════════════════════════════════════════');
  console.log('🤖 AUTO-TRADE BOT - ' + startTime.toISOString());
  console.log('════════════════════════════════════════════════════════════════');
  
  try {
    const wallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY);
    const proxyAddress = '0x0A9551d6C30c6E8C3d2948c8386077f246cece95';
    
    const tempClient = new ClobClient('https://clob.polymarket.com', 137, wallet);
    const creds = await tempClient.deriveApiKey();
    const client = new ClobClient('https://clob.polymarket.com', 137, wallet, creds, 2, proxyAddress);
    
    // Calculate NEXT 15-minute window (the one about to start)
    const now = Math.floor(Date.now() / 1000);
    const currentWindowStart = Math.floor(now / 900) * 900;
    const nextWindowStart = currentWindowStart + 900;
    const slug = 'eth-updown-15m-' + nextWindowStart;
    
    const windowTime = new Date(nextWindowStart * 1000);
    const windowEndTime = new Date((nextWindowStart + 900) * 1000);
    
    console.log('');
    console.log('🎯 Target market:', slug);
    console.log('   Window: ' + windowTime.toISOString().slice(11,16) + ' - ' + windowEndTime.toISOString().slice(11,16) + ' UTC');
    console.log('   Starts in:', Math.round((nextWindowStart - now) / 60), 'minutes');
    console.log('');
    
    // Fetch market
    const mktRes = await axios.get('https://gamma-api.polymarket.com/markets?slug=' + slug, { timeout: 15000 });
    const market = Array.isArray(mktRes.data) ? mktRes.data[0] : mktRes.data;
    
    if (!market) {
      console.log('❌ Market not found');
      await sendTelegramNotification('❌ Auto-trade failed: Market not found for ' + slug);
      return;
    }
    
    console.log('📊 ' + market.question);
    const prices = JSON.parse(market.outcomePrices || '[]');
    console.log('   Current prices: Up ' + prices[0] + ' | Down ' + prices[1]);
    
    const tokenIds = JSON.parse(market.clobTokenIds || '[]');
    
    let upSuccess = false, downSuccess = false;
    let upOrderId = '', downOrderId = '';
    
    // Place UP order
    console.log('');
    console.log('📝 Placing UP order: 5 @ /bin/bash.48...');
    try {
      const upOrder = await client.createAndPostOrder({
        tokenID: tokenIds[0],
        side: 'BUY',
        price: 0.48,
        size: 5,
      });
      
      if (upOrder.success) {
        upSuccess = true;
        upOrderId = upOrder.orderID;
        console.log('   ✅ UP placed! ID:', upOrderId?.slice(0, 20) + '...');
        logTrade({ slug, tokenId: tokenIds[0], side: 'BUY', outcome: 'Up', size: 5, price: 0.48, orderId: upOrderId });
      } else {
        console.log('   ❌ UP failed:', upOrder.error);
      }
    } catch (e) {
      console.log('   ❌ UP error:', e.response?.data?.error || e.message);
    }
    
    // Place DOWN order
    console.log('📝 Placing DOWN order: 5 @ /bin/bash.48...');
    try {
      const downOrder = await client.createAndPostOrder({
        tokenID: tokenIds[1],
        side: 'BUY',
        price: 0.48,
        size: 5,
      });
      
      if (downOrder.success) {
        downSuccess = true;
        downOrderId = downOrder.orderID;
        console.log('   ✅ DOWN placed! ID:', downOrderId?.slice(0, 20) + '...');
        logTrade({ slug, tokenId: tokenIds[1], side: 'BUY', outcome: 'Down', size: 5, price: 0.48, orderId: downOrderId });
      } else {
        console.log('   ❌ DOWN failed:', downOrder.error);
      }
    } catch (e) {
      console.log('   ❌ DOWN error:', e.response?.data?.error || e.message);
    }
    
    // Get balance
    const bal = await client.getBalanceAllowance({ asset_type: 'COLLATERAL' });
    const balance = (bal.balance / 1e6).toFixed(2);
    
    // Send Telegram notification
    const status = (upSuccess && downSuccess) ? '✅' : (upSuccess || downSuccess) ? '⚠️' : '❌';
    const msg = `${status} <b>Auto-Trade Executed</b>

📊 <b>${market.question}</b>
💹 Prices: Up ${prices[0]} | Down ${prices[1]}

📝 <b>Orders:</b>
• Up @ /bin/bash.48: ${upSuccess ? '✅ Placed' : '❌ Failed'}
• Down @ /bin/bash.48: ${downSuccess ? '✅ Placed' : '❌ Failed'}

💰 Balance: $${balance}
⏰ ${new Date().toISOString().slice(11,19)} UTC`;
    
    await sendTelegramNotification(msg);
    
    console.log('');
    console.log('════════════════════════════════════════════════════════════════');
    console.log(status + ' Complete | Balance: $' + balance);
    console.log('════════════════════════════════════════════════════════════════');
    
  } catch (e) {
    console.error('❌ Fatal error:', e.message);
    await sendTelegramNotification('❌ Auto-trade error: ' + e.message);
  }
}

placeAutoTrades();
