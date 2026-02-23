/**
 * Builder Relayer Redemption - With Auth Headers
 */

const axios = require('axios');
const crypto = require('crypto');
const { createWalletClient, createPublicClient, http, encodeFunctionData } = require('viem');
const { polygon } = require('viem/chains');
const { privateKeyToAccount } = require('viem/accounts');
const { hashTypedData } = require('viem');
const { ClobClient } = require('@polymarket/clob-client');
const { ethers } = require('ethers');
require('dotenv').config();

const RELAYER_URL = 'https://relayer-v2.polymarket.com';
const CTF = '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045';
const USDC = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const PROXY = '0x0A9551d6C30c6E8C3d2948c8386077f246cece95';

// Builder credentials
const BUILDER_KEY = process.env.BUILDER_API_KEY;
const BUILDER_SECRET = process.env.BUILDER_SECRET;
const BUILDER_PASSPHRASE = process.env.BUILDER_PASSPHRASE;

// HMAC signing
function signHmac(timestamp, method, path, body = '') {
  const message = timestamp + method + path + body;
  const secret = Buffer.from(BUILDER_SECRET.replace(/-/g, '+').replace(/_/g, '/'), 'base64');
  const hmac = crypto.createHmac('sha256', secret);
  hmac.update(message);
  return hmac.digest('base64').replace(/\+/g, '-').replace(/\//g, '_');
}

function getBuilderHeaders(method, path, body = '') {
  const ts = Math.floor(Date.now() / 1000).toString();
  return {
    'Content-Type': 'application/json',
    'POLY_BUILDER_API_KEY': BUILDER_KEY,
    'POLY_BUILDER_TIMESTAMP': ts,
    'POLY_BUILDER_PASSPHRASE': BUILDER_PASSPHRASE,
    'POLY_BUILDER_SIGNATURE': signHmac(ts, method, path, body)
  };
}

async function relayerPost(path, data) {
  const body = JSON.stringify(data);
  const headers = getBuilderHeaders('POST', path, body);
  const res = await axios.post(RELAYER_URL + path, body, { headers, timeout: 30000 });
  return res.data;
}

async function relayerGet(path) {
  const headers = getBuilderHeaders('GET', path);
  const res = await axios.get(RELAYER_URL + path, { headers, timeout: 15000 });
  return res.data;
}

// Create EIP-712 Safe transaction hash
function createSafeTxHash(chainId, safe, to, value, data, operation, nonce) {
  const domain = { chainId, verifyingContract: safe };
  const types = {
    SafeTx: [
      { name: 'to', type: 'address' },
      { name: 'value', type: 'uint256' },
      { name: 'data', type: 'bytes' },
      { name: 'operation', type: 'uint8' },
      { name: 'safeTxGas', type: 'uint256' },
      { name: 'baseGas', type: 'uint256' },
      { name: 'gasPrice', type: 'uint256' },
      { name: 'gasToken', type: 'address' },
      { name: 'refundReceiver', type: 'address' },
      { name: 'nonce', type: 'uint256' },
    ],
  };
  const message = {
    to, value: BigInt(value), data, operation,
    safeTxGas: 0n, baseGas: 0n, gasPrice: 0n,
    gasToken: '0x0000000000000000000000000000000000000000',
    refundReceiver: '0x0000000000000000000000000000000000000000',
    nonce: BigInt(nonce),
  };
  return hashTypedData({ domain, types, primaryType: 'SafeTx', message });
}

async function main() {
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('         BUILDER RELAYER REDEMPTION - ' + new Date().toISOString().slice(0, 19));
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('');

  if (!BUILDER_KEY || !BUILDER_SECRET) {
    console.log('❌ Missing builder credentials');
    return;
  }

  // Setup viem
  let pk = process.env.POLYMARKET_PRIVATE_KEY;
  if (!pk.startsWith('0x')) pk = '0x' + pk;
  
  const account = privateKeyToAccount(pk);
  console.log('Signer:', account.address);
  console.log('Proxy:', PROXY);

  const publicClient = createPublicClient({
    chain: polygon,
    transport: http('https://polygon-rpc.com')
  });

  // Balance before
  const balBefore = await publicClient.readContract({
    address: USDC,
    abi: [{ name: 'balanceOf', type: 'function', inputs: [{ name: 'account', type: 'address' }], outputs: [{ type: 'uint256' }] }],
    functionName: 'balanceOf',
    args: [PROXY]
  });
  console.log('💰 Balance:', Number(balBefore) / 1e6, 'USDC');

  // Test relayer connection
  const relayAddr = await relayerGet('/address');
  console.log('✅ Relayer:', relayAddr.address);
  console.log('');

  // Get resolved markets
  const ethersWallet = new ethers.Wallet(process.env.POLYMARKET_PRIVATE_KEY);
  const tempClient = new ClobClient('https://clob.polymarket.com', 137, ethersWallet);
  const creds = await tempClient.deriveApiKey();
  const client = new ClobClient('https://clob.polymarket.com', 137, ethersWallet, creds, 2, PROXY);

  const trades = await client.getTrades();
  const marketIds = [...new Set(trades.map(t => t.market))];
  const resolved = [];
  
  for (const marketId of marketIds.slice(0, 50)) {
    try {
      const market = await client.getMarket(marketId);
      if (market?.closed && market?.condition_id) resolved.push(market.condition_id);
    } catch (e) {}
  }

  console.log('📊', resolved.length, 'resolved markets');
  console.log('');

  const CTF_ABI = [{
    name: 'redeemPositions', type: 'function',
    inputs: [
      { name: 'collateralToken', type: 'address' },
      { name: 'parentCollectionId', type: 'bytes32' },
      { name: 'conditionId', type: 'bytes32' },
      { name: 'indexSets', type: 'uint256[]' }
    ]
  }];

  let success = 0;

  for (const conditionId of resolved.slice(0, 5)) {
    console.log('Redeeming:', conditionId.slice(0, 14) + '...');

    try {
      // Get nonce
      const nonceRes = await relayerGet('/nonce?address=' + account.address + '&type=SAFE');
      const nonce = parseInt(nonceRes.nonce);

      // Encode call
      const data = encodeFunctionData({
        abi: CTF_ABI,
        functionName: 'redeemPositions',
        args: [USDC, '0x' + '0'.repeat(64), conditionId, [1n, 2n]]
      });

      // Create Safe TX hash (EIP-712)
      const txHash = createSafeTxHash(137, PROXY, CTF, '0', data, 0, nonce);

      // Sign
      const signature = await account.signMessage({ message: { raw: txHash } });
      
      // Pack signature (add 4 to v for eth_sign)
      const r = signature.slice(0, 66);
      const s = '0x' + signature.slice(66, 130);
      const v = parseInt(signature.slice(130, 132), 16) + 4;
      const packedSig = r + s.slice(2) + v.toString(16).padStart(2, '0');

      // Submit
      const req = {
        from: account.address,
        to: CTF,
        proxyWallet: PROXY,
        data: data,
        nonce: nonce.toString(),
        signature: packedSig,
        signatureParams: {
          gasPrice: '0', operation: '0', safeTxnGas: '0', baseGas: '0',
          gasToken: '0x0000000000000000000000000000000000000000',
          refundReceiver: '0x0000000000000000000000000000000000000000'
        },
        type: 'SAFE',
        metadata: ''
      };

      const result = await relayerPost('/submit', req);
      console.log('   TX:', result.transactionID);
      console.log('   State:', result.state);
      success++;

    } catch (e) {
      const errData = e.response?.data;
      console.log('   ❌', errData?.error || e.message?.slice(0, 40));
    }

    await new Promise(r => setTimeout(r, 2000));
  }

  // Balance after
  await new Promise(r => setTimeout(r, 5000));
  const balAfter = await publicClient.readContract({
    address: USDC,
    abi: [{ name: 'balanceOf', type: 'function', inputs: [{ name: 'account', type: 'address' }], outputs: [{ type: 'uint256' }] }],
    functionName: 'balanceOf',
    args: [PROXY]
  });

  console.log('');
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('💰 After:', Number(balAfter) / 1e6, 'USDC');
  console.log('Success:', success);
  console.log('═══════════════════════════════════════════════════════════════════');
}

main().catch(e => console.error('Fatal:', e.message));
