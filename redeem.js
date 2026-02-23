/**
 * Polymarket CTF Redemption Script
 * Usage: node redeem.js
 * 
 * Redeems winning positions from resolved markets using the official
 * CTF redeemPositions method per docs.polymarket.com
 */

const { ethers } = require('ethers');
const { ClobClient } = require('@polymarket/clob-client');
require('dotenv').config();

// Contracts
const CTF = '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045';
const USDC = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';

const CTF_ABI = [
  'function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets) external',
  'function payoutDenominator(bytes32 conditionId) view returns (uint256)'
];

const SAFE_ABI = [
  'function nonce() view returns (uint256)',
  'function getTransactionHash(address to, uint256 value, bytes data, uint8 operation, uint256 safeTxGas, uint256 baseGas, uint256 gasPrice, address gasToken, address refundReceiver, uint256 _nonce) view returns (bytes32)',
  'function execTransaction(address to, uint256 value, bytes data, uint8 operation, uint256 safeTxGas, uint256 baseGas, uint256 gasPrice, address gasToken, address refundReceiver, bytes signatures) payable returns (bool)'
];

async function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function main() {
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('              POLYMARKET CTF REDEMPTION');
  console.log('═══════════════════════════════════════════════════════════════════\n');

  const provider = new ethers.providers.JsonRpcProvider('https://polygon-rpc.com');
  const wallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY, provider);
  const proxyAddress = '0x0A9551d6C30c6E8C3d2948c8386077f246cece95';

  console.log('Proxy:', proxyAddress);
  
  // Balance before
  const usdc = new ethers.Contract(USDC, ['function balanceOf(address) view returns (uint256)'], provider);
  const balBefore = await usdc.balanceOf(proxyAddress);
  console.log('💰 Balance before:', ethers.utils.formatUnits(balBefore, 6), 'USDC\n');

  // Get resolved markets from CLOB
  const tempClient = new ClobClient('https://clob.polymarket.com', 137, wallet);
  const creds = await tempClient.deriveApiKey();
  const client = new ClobClient('https://clob.polymarket.com', 137, wallet, creds, 2, proxyAddress);

  const trades = await client.getTrades();
  const marketIds = [...new Set(trades.map(t => t.market))];

  const resolved = [];
  for (const marketId of marketIds) {
    try {
      const market = await client.getMarket(marketId);
      if (market && market.closed && market.condition_id) {
        resolved.push(market.condition_id);
      }
    } catch (e) {}
    await sleep(50);
  }

  console.log('📊 Found', resolved.length, 'resolved markets\n');

  const ctf = new ethers.Contract(CTF, CTF_ABI, provider);
  const proxy = new ethers.Contract(proxyAddress, SAFE_ABI, wallet);

  let success = 0, skipped = 0;

  for (const conditionId of resolved) {
    process.stdout.write('Redeeming ' + conditionId.slice(0, 12) + '... ');

    try {
      await sleep(300);
      const denom = await ctf.payoutDenominator(conditionId);
      if (denom.eq(0)) {
        console.log('⏳ not resolved');
        continue;
      }

      // Encode CTF redeemPositions call
      const redeemData = ctf.interface.encodeFunctionData('redeemPositions', [
        USDC,
        ethers.constants.HashZero,
        conditionId,
        [1, 2]
      ]);

      await sleep(500);
      const nonce = await proxy.nonce();

      await sleep(500);
      const txHash = await proxy.getTransactionHash(
        CTF, 0, redeemData, 0, 0, 0, 0,
        ethers.constants.AddressZero, ethers.constants.AddressZero, nonce
      );

      const sig = await wallet.signMessage(ethers.utils.arrayify(txHash));
      const split = ethers.utils.splitSignature(sig);
      const packed = ethers.utils.solidityPack(['bytes32', 'bytes32', 'uint8'], [split.r, split.s, split.v + 4]);

      await sleep(500);
      const tx = await proxy.execTransaction(
        CTF, 0, redeemData, 0, 0, 0, 0,
        ethers.constants.AddressZero, ethers.constants.AddressZero, packed,
        { gasLimit: 500000 }
      );

      console.log('TX: ' + tx.hash.slice(0, 18) + '...');
      await tx.wait();
      console.log('   ✅ Confirmed!');
      success++;

    } catch (e) {
      const msg = e.reason || e.message || '';
      if (msg.includes('revert')) {
        console.log('⏭️ no tokens');
        skipped++;
      } else if (msg.includes('network')) {
        console.log('⚠️ network error');
      } else {
        console.log('❌ ' + msg.slice(0, 40));
      }
    }

    await sleep(1500);
  }

  // Balance after
  await sleep(3000);
  const balAfter = await usdc.balanceOf(proxyAddress);

  console.log('\n═══════════════════════════════════════════════════════════════════');
  console.log('💰 Balance after:', ethers.utils.formatUnits(balAfter, 6), 'USDC');
  
  const diff = balAfter.sub(balBefore);
  if (diff.gt(0)) {
    console.log('✅ Redeemed:', ethers.utils.formatUnits(diff, 6), 'USDC');
  }
  console.log('Results:', success, 'success,', skipped, 'skipped');
  console.log('═══════════════════════════════════════════════════════════════════');
}

main().catch(e => console.error('Fatal:', e.message));
