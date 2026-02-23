const { ClobClient } = require('@polymarket/clob-client');
const { ethers } = require('ethers');
const fs = require('fs');
require('dotenv').config();

async function checkStatus() {
  try {
    const wallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY);
    const proxyAddress = '0x0A9551d6C30c6E8C3d2948c8386077f246cece95';
    
    const tempClient = new ClobClient('https://clob.polymarket.com', 137, wallet);
    const creds = await tempClient.deriveApiKey();
    const client = new ClobClient('https://clob.polymarket.com', 137, wallet, creds, 2, proxyAddress);

    // Get balance
    const bal = await client.getBalanceAllowance({ asset_type: 'COLLATERAL' });
    const balance = parseFloat(bal.balance) / 1e6;
    
    console.log('BALANCE:', balance.toFixed(2));
    
    // Read previous state
    let previousState = { lastBalance: 17.6, bigPositionFound: false };
    try {
      previousState = JSON.parse(fs.readFileSync('monitor-state.json', 'utf8'));
    } catch (e) {
      console.log('NO_PREVIOUS_STATE');
    }
    
    // Update state
    const newState = {
      lastBalance: balance,
      lastCheck: new Date().toISOString(),
      bigPositionFound: false
    };
    fs.writeFileSync('monitor-state.json', JSON.stringify(newState, null, 2));
    
    // Check if we should notify
    const balanceChange = Math.abs(balance - previousState.lastBalance);
    const bigPositionResolved = previousState.bigPositionFound && !newState.bigPositionFound;
    
    console.log('PREVIOUS_BALANCE:', previousState.lastBalance.toFixed(2));
    console.log('BALANCE_CHANGE:', balanceChange.toFixed(2));
    console.log('BIG_POSITION_RESOLVED:', bigPositionResolved);
    console.log('SHOULD_NOTIFY:', balanceChange > 5 || bigPositionResolved);
    
  } catch (error) {
    console.error('ERROR:', error.message);
    process.exit(1);
  }
}

checkStatus();
