/**
 * Robust Auto-Redemption v3
 */

const { ethers } = require('ethers');
const { ClobClient } = require('@polymarket/clob-client');
require('dotenv').config();

const RPCS = [
  'https://polygon-rpc.com',
  'https://rpc.ankr.com/polygon',
  'https://polygon.llamarpc.com',
  'https://1rpc.io/matic',
  'https://polygon-mainnet.public.blastapi.io'
];

const CTF = '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045';
const USDC = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';

const SAFE_ABI = [
  'function nonce() view returns (uint256)',
  'function getTransactionHash(address to, uint256 value, bytes data, uint8 operation, uint256 safeTxGas, uint256 baseGas, uint256 gasPrice, address gasToken, address refundReceiver, uint256 _nonce) view returns (bytes32)',
  'function execTransaction(address to, uint256 value, bytes data, uint8 operation, uint256 safeTxGas, uint256 baseGas, uint256 gasPrice, address gasToken, address refundReceiver, bytes signatures) payable returns (bool)'
];

let currentRpcIndex = 0;

function getNextRpc() {
  const rpc = RPCS[currentRpcIndex];
  currentRpcIndex = (currentRpcIndex + 1) % RPCS.length;
  return rpc;
}

async function getWorkingProvider() {
  for (let i = 0; i < RPCS.length * 2; i++) {
    const rpc = getNextRpc();
    try {
      const p = new ethers.providers.JsonRpcProvider({ url: rpc, timeout: 10000 });
      await Promise.race([
        p.getBlockNumber(),
        new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 5000))
      ]);
      return { provider: p, rpc };
    } catch (e) {
      await new Promise(r => setTimeout(r, 500));
    }
  }
  throw new Error('All RPCs failed');
}

async function retry(fn, maxAttempts = 3, delay = 2000) {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      return await fn();
    } catch (e) {
      if (i === maxAttempts - 1) throw e;
      await new Promise(r => setTimeout(r, delay));
    }
  }
}

async function main() {
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('            AUTO-REDEMPTION - ' + new Date().toISOString().slice(0, 19));
  console.log('═══════════════════════════════════════════════════════════════════');

  let provider, rpc;
  try {
    ({ provider, rpc } = await getWorkingProvider());
    console.log('✅ RPC:', rpc);
  } catch (e) {
    console.log('❌ All RPCs failed');
    return;
  }

  const wallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY, provider);
  const proxyAddress = '0x0A9551d6C30c6E8C3d2948c8386077f246cece95';

  // Get resolved markets from CLOB
  let resolved = [];
  try {
    const tempClient = new ClobClient('https://clob.polymarket.com', 137, wallet);
    const creds = await tempClient.deriveApiKey();
    const client = new ClobClient('https://clob.polymarket.com', 137, wallet, creds, 2, proxyAddress);

    const trades = await client.getTrades();
    const marketIds = [...new Set(trades.map(t => t.market))];

    for (const marketId of marketIds.slice(0, 50)) {
      try {
        const market = await client.getMarket(marketId);
        if (market && market.closed && market.condition_id) {
          resolved.push(market.condition_id);
        }
      } catch (e) {}
    }
  } catch (e) {
    console.log('❌ CLOB error:', e.message?.slice(0, 50));
    return;
  }

  console.log('📊 ' + resolved.length + ' resolved markets');

  if (resolved.length === 0) {
    console.log('Nothing to redeem');
    return;
  }

  // Refresh provider
  try {
    ({ provider, rpc } = await getWorkingProvider());
  } catch (e) {
    console.log('❌ Lost RPC');
    return;
  }

  // Balance before
  const usdc = new ethers.Contract(USDC, ['function balanceOf(address) view returns (uint256)'], provider);
  let balBefore;
  try {
    balBefore = await retry(() => usdc.balanceOf(proxyAddress));
    console.log('💰 Balance:', ethers.utils.formatUnits(balBefore, 6), 'USDC');
  } catch (e) {
    console.log('❌ Balance check failed');
    return;
  }

  const proxy = new ethers.Contract(proxyAddress, SAFE_ABI, wallet);
  const ctfInterface = new ethers.utils.Interface([
    'function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)'
  ]);

  let success = 0, skipped = 0, failed = 0;

  for (const conditionId of resolved.slice(0, 8)) {
    process.stdout.write(conditionId.slice(0, 10) + '... ');

    try {
      // Reconnect if needed
      try {
        await provider.getBlockNumber();
      } catch (e) {
        ({ provider, rpc } = await getWorkingProvider());
        console.log('🔄');
      }

      const redeemData = ctfInterface.encodeFunctionData('redeemPositions', [
        USDC, ethers.constants.HashZero, conditionId, [1, 2]
      ]);

      const nonce = await retry(() => proxy.nonce());
      const txHash = await retry(() => proxy.getTransactionHash(
        CTF, 0, redeemData, 0, 0, 0, 0,
        ethers.constants.AddressZero, ethers.constants.AddressZero, nonce
      ));

      const sig = await wallet.signMessage(ethers.utils.arrayify(txHash));
      const split = ethers.utils.splitSignature(sig);
      const packed = ethers.utils.solidityPack(['bytes32', 'bytes32', 'uint8'], [split.r, split.s, split.v + 4]);

      const tx = await proxy.execTransaction(
        CTF, 0, redeemData, 0, 0, 0, 0,
        ethers.constants.AddressZero, ethers.constants.AddressZero, packed,
        { gasLimit: 400000 }
      );

      const receipt = await tx.wait(1);
      if (receipt.status === 1) {
        console.log('✅');
        success++;
      } else {
        console.log('❌');
        failed++;
      }

    } catch (e) {
      const msg = e.reason || e.message || '';
      if (msg.includes('revert')) {
        console.log('⏭️');
        skipped++;
      } else if (msg.includes('network')) {
        console.log('🔌');
        failed++;
      } else {
        console.log('❌');
        failed++;
      }
    }

    await new Promise(r => setTimeout(r, 2000));
  }

  // Final balance
  console.log('');
  try {
    ({ provider, rpc } = await getWorkingProvider());
    const balAfter = await retry(() => usdc.balanceOf(proxyAddress));
    console.log('═══════════════════════════════════════════════════════════════════');
    console.log('💰 After:', ethers.utils.formatUnits(balAfter, 6), 'USDC');
    const diff = balAfter.sub(balBefore);
    if (diff.gt(0)) console.log('✅ Claimed:', ethers.utils.formatUnits(diff, 6));
  } catch (e) {}
  
  console.log('Results: ' + success + '✅ ' + skipped + '⏭️ ' + failed + '❌');
  console.log('═══════════════════════════════════════════════════════════════════');
}

main().catch(e => console.error('Fatal:', e.message));
