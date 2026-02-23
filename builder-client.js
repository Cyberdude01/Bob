/**
 * Builder Relayer Client using viem
 */

const { createWalletClient, createPublicClient, http } = require('viem');
const { polygon } = require('viem/chains');
const { privateKeyToAccount } = require('viem/accounts');
const { RelayClient } = require('@polymarket/relayer-client');
const { ClobClient } = require('@polymarket/clob-client');
const { ethers } = require('ethers');
require('dotenv').config();

const CTF = '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045';
const USDC = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';

async function main() {
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('            BUILDER RELAYER - VIEM INTEGRATION');
  console.log('═══════════════════════════════════════════════════════════════════');
  console.log('');

  const privateKey = process.env.POLYMARKET_PRIVATE_KEY;
  if (!privateKey.startsWith('0x')) {
    process.env.POLYMARKET_PRIVATE_KEY = '0x' + privateKey;
  }

  // Create viem account
  const account = privateKeyToAccount(process.env.POLYMARKET_PRIVATE_KEY);
  console.log('✅ Viem account:', account.address);

  // Create wallet client (for signing)
  const walletClient = createWalletClient({
    account,
    chain: polygon,
    transport: http('https://polygon-rpc.com')
  });

  // Create public client (for reading)
  const publicClient = createPublicClient({
    chain: polygon,
    transport: http('https://polygon-rpc.com')
  });

  console.log('✅ Viem clients created');

  // Test: get balance
  const balance = await publicClient.readContract({
    address: USDC,
    abi: [{ name: 'balanceOf', type: 'function', inputs: [{ name: 'account', type: 'address' }], outputs: [{ type: 'uint256' }] }],
    functionName: 'balanceOf',
    args: ['0x0A9551d6C30c6E8C3d2948c8386077f246cece95']
  });
  console.log('💰 USDC Balance:', Number(balance) / 1e6);

  // Initialize RelayClient with viem wallet
  try {
    const relayer = new RelayClient(
      'https://relayer-v2.polymarket.com',
      137,
      walletClient
    );
    console.log('✅ RelayClient initialized');

    // Test get address
    const addr = await relayer.getRelayAddress();
    console.log('📍 Relayer address:', addr.address);

    // Test get nonce
    const nonce = await relayer.getNonce(account.address, 'SAFE');
    console.log('📋 Safe nonce:', nonce.nonce);

  } catch (e) {
    console.log('❌ RelayClient error:', e.message);
  }
}

main().catch(e => console.error('Fatal:', e.message));
