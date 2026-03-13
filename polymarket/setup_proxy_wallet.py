"""
Polymarket Proxy Wallet Deployer
=================================
Deploys the deterministic Non-Safe Proxy Wallet for an EOA on Polygon.

Polymarket uses sig_type=1 (POLY_PROXY) orders whose maker address is the proxy
wallet.  The proxy wallet address is derived deterministically from the EOA via
CREATE2.  If no proxy has been deployed yet the CLI's L2 order signing returns
"invalid signature" because the CTF Exchange cannot call isAuthorizedSigner() on
an EOA.

This script:
  1. Computes the proxy wallet address (CREATE2, no RPC needed)
  2. Checks whether the contract is already live
  3. If not, calls ProxyWalletFactory.proxy([]) to deploy it (costs ~50k gas)
  4. Prints the POLY_ADDRESS value to add to /etc/polymarket.env

Run once:
  python -m polymarket.setup_proxy_wallet

Requires: POLY_PRIVATE_KEY (and POLYGON_RPC_URL optional) in /etc/polymarket.env
"""
import os
import sys
from dotenv import load_dotenv
from web3 import Web3

load_dotenv("/etc/polymarket.env")
load_dotenv()

# ── Polymarket proxy wallet constants (Polygon mainnet) ──────────────────────
# Source: https://github.com/Polymarket/magic-proxy-builder-example/blob/main/constants/proxyWallet.ts
PROXY_FACTORY      = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_INIT_CODE_HASH = "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"

# ── Factory ABI — only the proxy() function is needed ───────────────────────
# ProxyWalletLib.ProxyCall struct has fields: to (address), value (uint256),
# data (bytes).  We call with an empty array so the struct shape only matters
# for selector computation; with [] the encoding is the same regardless.
FACTORY_ABI = [
    {
        "name": "proxy",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "to",    "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data",  "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {"name": "returnValues", "type": "bytes[]"}
        ],
    }
]

GREEN = "\033[92m✔\033[0m"
RED   = "\033[91m✖\033[0m"
INFO  = "\033[94mi\033[0m"


def compute_proxy_address(w3: Web3, eoa: str) -> str:
    """
    CREATE2 address = keccak256(0xff | factory | salt | initCodeHash)[12:]
    where salt = keccak256(abi.encodePacked(eoa))
    """
    eoa_bytes     = bytes.fromhex(eoa[2:])          # 20-byte packed encoding
    salt          = w3.keccak(eoa_bytes)             # bytes32
    factory_bytes = bytes.fromhex(PROXY_FACTORY[2:])
    init_code_hash= bytes.fromhex(PROXY_INIT_CODE_HASH[2:])
    preimage      = b"\xff" + factory_bytes + salt + init_code_hash
    raw           = w3.keccak(preimage)[12:]         # last 20 bytes
    return w3.to_checksum_address("0x" + raw.hex())


def main():
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not private_key:
        print(f"  {RED}  POLY_PRIVATE_KEY not set"); sys.exit(1)

    _rpc_candidates = [
        os.environ.get("POLYGON_RPC_URL", ""),
        "https://rpc.ankr.com/polygon/b77f5e0dd955373ca4c9e3d668d87d8217aaa907b55aa39d430ccc686b78fe22",
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://1rpc.io/matic",
        "https://polygon.llamarpc.com",
    ]
    w3 = None
    for _rpc in _rpc_candidates:
        if not _rpc:
            continue
        try:
            _w3 = Web3(Web3.HTTPProvider(_rpc, request_kwargs={"timeout": 10}))
            _ = _w3.eth.block_number
            w3 = _w3
            print(f"  {INFO}  RPC:    {_rpc}")
            break
        except Exception:
            continue
    if w3 is None:
        print(f"  {RED}  All Polygon RPC endpoints failed"); sys.exit(1)

    from eth_account import Account
    acct = Account.from_key(private_key)
    eoa  = acct.address
    print(f"  {INFO}  EOA:    {eoa}")
    print(f"  {INFO}  chain:  {w3.eth.chain_id}  (block {w3.eth.block_number})")

    proxy_addr = compute_proxy_address(w3, eoa)
    print(f"  {INFO}  proxy:  {proxy_addr}")

    # Check if already deployed
    code = w3.eth.get_code(proxy_addr)
    if code and code != b"":
        print(f"  {GREEN}  Proxy wallet already deployed at {proxy_addr}")
        print(f"\n  Add to /etc/polymarket.env:")
        print(f"  POLY_ADDRESS={proxy_addr}")
        return

    # Check POL balance for gas
    pol_balance = w3.eth.get_balance(eoa)
    print(f"  {INFO}  POL balance: {pol_balance / 1e18:.4f}")
    if pol_balance < w3.to_wei("0.01", "ether"):
        print(f"  {RED}  Need at least 0.01 POL for deployment gas (have {pol_balance/1e18:.4f})")
        sys.exit(1)

    # Deploy proxy by calling factory.proxy([])
    print(f"\n  Deploying proxy wallet via ProxyWalletFactory.proxy([]) ...")
    factory = w3.eth.contract(
        address=w3.to_checksum_address(PROXY_FACTORY),
        abi=FACTORY_ABI,
    )
    tx = factory.functions.proxy([]).build_transaction({
        "from":                 eoa,
        "nonce":                w3.eth.get_transaction_count(eoa),
        "gas":                  200_000,
        "maxFeePerGas":         w3.to_wei("150", "gwei"),
        "maxPriorityFeePerGas": w3.to_wei("30",  "gwei"),
        "chainId":              137,
        "value":                0,
    })
    signed  = w3.eth.account.sign_transaction(tx, acct.key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  {INFO}  tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        print(f"  {RED}  Transaction reverted — check gas / factory state")
        sys.exit(1)

    # Verify deployment
    code = w3.eth.get_code(proxy_addr)
    if code and code != b"":
        print(f"  {GREEN}  Proxy wallet deployed successfully!")
    else:
        print(f"  {RED}  Transaction succeeded but no code at {proxy_addr} — unexpected")
        print(f"  {INFO}  The factory may use a different salt formula.  Check the tx on PolygonScan:")
        print(f"  {INFO}  https://polygonscan.com/tx/{tx_hash.hex()}")
        sys.exit(1)

    print(f"\n  {GREEN}  Done!")
    print(f"\n  Update /etc/polymarket.env — replace POLY_ADDRESS with the proxy wallet:")
    print(f"\n  POLY_ADDRESS={proxy_addr}")
    print(f"\n  Then run setup_allowance.py again (USDC/CTF approvals must come from")
    print(f"  the proxy wallet, not the EOA, if using sig_type=1).")


if __name__ == "__main__":
    main()
