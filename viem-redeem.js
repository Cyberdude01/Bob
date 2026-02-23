/**
 * Viem-based Builder Relayer Redemption
 * Properly signs Safe transactions with EIP-712
 */

const axios = require('axios');
const crypto = require('crypto');
const { createPublicClient, http, encodeFunctionData, keccak256, encodeAbiParameters, parseAbiParameters, concat, toHex, pad, hexToBytes, bytesToHex } = require('viem');
const { polygon } = require('viem/chains');
const { privateKeyToAccount } = require('viem/accounts');
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

// HMAC signing for builder API
function signHmac(timestamp, method, path, body = '') {
  const message = timestamp + method + path + body;
  const secret = Buffer.from(BUILDER_SECRET.replace(/-/g, '+').replace(/_/g, '/'), 'base64');
  return crypto.createHmac('sha256', secret).update(message).digest('base64').replace(/\+/g, '-').replace(/\//g, '_');
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
  return (await axios.post(RELAYER_URL + path, body, { headers, timeout: 30000 })).data;
}

async function relayerGet(path) {
  const headers = getBuilderHeaders('GET', path);
  return (await axios.get(RELAYER_URL + path, { headers, timeout: 15000 })).data;
}

// Gnosis Safe EIP-712 domain and types
const SAFE_TX_TYPEHASH = keccak256(
  toHex('SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)')
);

const DOMAIN_SEPARATOR_TYPEHASH = keccak256(
  toHex('EIP712Domain(uint256 chainId,address verifyingContract)')
);

function calculateDomainSeparator(chainId, safeAddress) {
  return keccak256(
    encodeAbiParameters(
      parseAbiParameters('bytes32, uint256, address'),
      [DOMAIN_SEPARATOR_TYPEHASH, BigInt(chainId), safeAddress]
    )
  );
}

function calculateSafeTxHash(chainId, safeAddress, to, value, data, operation, nonce) {
  const domainSeparator = calculateDomainSeparator(chainId, safeAddress);
  
  const dataHash = keccak256(data);
  
  const safeTxHash = keccak256(
    encodeAbiParameters(
      parseAbiParameters('bytes32, address, uint256, bytes32, uint8, uint256, uint256, uint256, address, address, uint256'),
      [
        SAFE_TX_TYPEHASH,
        to,
        BigInt(value),
        dataHash,
        operation,
        0n, // safeTxGas
        0n, // baseGas
        0n, // gasPrice
        '0x0000000000000000000000000000000000000000', // gasToken
        '0x0000000000000000000000000000000000000000', // refundReceiver
        BigInt(nonce)
      ]
    )
  );
  
  // EIP-712 hash = keccak256("\x19\x01" + domainSeparator + structHash)
  const encoded = concat(['0x1901', domainSeparator, safeTxHash]);
  return keccak256(encoded);
}

async function main() {
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('         VIEM BUILDER REDEMPTION - ' + new Date().toISOString().slice(0, 19));
  console.log('═══════════════════════════════════════════════════════════════════\n');

  if (!BUILDER_KEY || !BUILDER_SECRET || !BUILDER_PASSPHRASE) {
    console.log('❌ Missing builder credentials in .env');
    return;
  }

  // Setup viem account
  let pk = process.env.POLYMARKET_PRIVATE_KEY;
  if (!pk.startsWith('0x')) pk = '0x' + pk;
  const account = privateKeyToAccount(pk);
  
  console.log('Signer:', account.address);
  console.log('Proxy:', PROXY);

  const publicClient = createPublicClient({
    chain: polygon,
    transport: http('https://polygon-mainnet.infura.io/v3/fef2edcc615944ca816d857741ef9d5d')
  });

  // Check balance
  const balBefore = await publicClient.readContract({
    address: USDC,
    abi: [{ name: 'balanceOf', type: 'function', stateMutability: 'view', inputs: [{ name: 'account', type: 'address' }], outputs: [{ type: 'uint256' }] }],
    functionName: 'balanceOf',
    args: [PROXY]
  });
  console.log('💰 Balance:', (Number(balBefore) / 1e6).toFixed(2), 'USDC');

  // Test relayer connection
  try {
    const relayAddr = await relayerGet('/address');
    console.log('✅ Relayer:', relayAddr.address);
  } catch (e) {
    console.log('❌ Relayer connection failed:', e.response?.data?.error || e.message);
    return;
  }

  // Get nonce
  const nonceRes = await relayerGet('/nonce?address=' + account.address + '&type=SAFE');
  console.log('📝 Safe nonce:', nonceRes.nonce);
  console.log('');

  // Get resolved markets using ethers ClobClient
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
      if (market?.closed && market?.condition_id) {
        resolved.push(market.condition_id);
      }
    } catch (e) {}
  }

  console.log('📊 Found', resolved.length, 'resolved markets\n');

  const CTF_ABI = [{
    name: 'redeemPositions',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [
      { name: 'collateralToken', type: 'address' },
      { name: 'parentCollectionId', type: 'bytes32' },
      { name: 'conditionId', type: 'bytes32' },
      { name: 'indexSets', type: 'uint256[]' }
    ],
    outputs: []
  }];

  let success = 0;
  let nonce = parseInt(nonceRes.nonce);

  for (const conditionId of resolved) {
    console.log('Redeeming:', conditionId.slice(0, 16) + '...');

    try {
      // Encode call data
      const data = encodeFunctionData({
        abi: CTF_ABI,
        functionName: 'redeemPositions',
        args: [
          USDC,
          '0x0000000000000000000000000000000000000000000000000000000000000000',
          conditionId,
          [1n, 2n]
        ]
      });

      // Calculate Safe transaction hash (EIP-712)
      const safeTxHash = calculateSafeTxHash(137, PROXY, CTF, '0', data, 0, nonce);
      console.log('   Hash:', safeTxHash.slice(0, 18) + '...');

      // Sign with signMessage (eth_sign style, as required by Safe)
      // For eth_sign compatibility, we sign the raw hash with the Ethereum prefix
      const signature = await account.signMessage({ message: { raw: hexToBytes(safeTxHash) } });
      
      // Adjust v value: eth_sign uses v + 4 for Safe signature type
      const sigBytes = hexToBytes(signature);
      sigBytes[64] = sigBytes[64] + 4;
      const adjustedSig = bytesToHex(sigBytes);

      console.log('   Sig:', adjustedSig.slice(0, 18) + '...');

      // Submit to relayer
      const request = {
        from: account.address,
        to: CTF,
        proxyWallet: PROXY,
        data: data,
        nonce: nonce.toString(),
        signature: adjustedSig,
        signatureParams: {
          gasPrice: '0',
          operation: '0',
          safeTxnGas: '0',
          baseGas: '0',
          gasToken: '0x0000000000000000000000000000000000000000',
          refundReceiver: '0x0000000000000000000000000000000000000000'
        },
        type: 'SAFE',
        metadata: ''
      };

      const result = await relayerPost('/submit', request);
      console.log('   ✅ TX:', result.transactionID);
      console.log('   State:', result.state);
      success++;
      nonce++;

    } catch (e) {
      const errData = e.response?.data;
      console.log('   ❌', errData?.error || e.message?.slice(0, 60));
    }

    await new Promise(r => setTimeout(r, 3000));
  }

  // Check balance after
  await new Promise(r => setTimeout(r, 5000));
  const balAfter = await publicClient.readContract({
    address: USDC,
    abi: [{ name: 'balanceOf', type: 'function', stateMutability: 'view', inputs: [{ name: 'account', type: 'address' }], outputs: [{ type: 'uint256' }] }],
    functionName: 'balanceOf',
    args: [PROXY]
  });

  console.log('\n═══════════════════════════════════════════════════════════════════');
  console.log('💰 After:', (Number(balAfter) / 1e6).toFixed(2), 'USDC');
  console.log('Success:', success, '/', resolved.length);
  console.log('═══════════════════════════════════════════════════════════════════');
}

main().catch(e => console.error('Fatal:', e.message));
