"""
Utility to extract the first 8-byte discriminators from transaction instructions.

Usage:
    python extract_ix_hashes.py <TX_SIGNATURE> [<TX_SIGNATURE>...]

Environment:
    SOLANA_RPC_URL - RPC endpoint (defaults to mainnet-beta)
"""

import base64
import sys
import os
from solana.rpc.api import Client


def extract_from_sig(client: Client, sig: str):
    resp = client.get_transaction(sig, encoding="jsonParsed", max_supported_transaction_version=0)
    tx = (resp.get("result") or {}).get("transaction") or {}
    message = tx.get("message") or {}
    account_keys = message.get("accountKeys") or []
    instructions = message.get("instructions") or []
    out = []
    for ix in instructions:
        program_idx = ix.get("programIdIndex")
        data = ix.get("data")
        if program_idx is None or data is None:
            continue
        try:
            raw = base64.b64decode(data)
            discr = raw[:8].hex()
        except Exception:
            discr = "decode_error"
        program = account_keys[program_idx] if program_idx < len(account_keys) else "?"
        out.append({"program": program, "discriminator_hex": discr})
    return out


def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_ix_hashes.py <TX_SIGNATURE> [<TX_SIGNATURE>...]")
        sys.exit(1)
    rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    client = Client(rpc_url)
    for sig in sys.argv[1:]:
        print(f"=== {sig} ===")
        try:
            rows = extract_from_sig(client, sig)
            for row in rows:
                print(f"{row['program']}: {row['discriminator_hex']}")
        except Exception as e:
            print(f"Error {sig}: {e}")


if __name__ == "__main__":
    main()


