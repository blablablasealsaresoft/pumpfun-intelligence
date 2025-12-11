"""
Simplified Solana API wrapper that handles response objects correctly
"""

from solana.rpc.api import Client
from solders.pubkey import Pubkey
import os
import json

class SolanaAPI:
    def __init__(self, rpc_url: str, ws_url: str = None):
        self.rpc_url = rpc_url
        self.ws_url = ws_url
        self.rpc_client = Client(rpc_url)

    def get_sol_balance(self, public_key: str) -> float:
        """Get SOL balance for a wallet"""
        try:
            pub_key = Pubkey.from_string(public_key)
            response = self.rpc_client.get_balance(pub_key)
            # Handle response object
            balance_lamports = response.value if hasattr(response, 'value') else response
            return balance_lamports / 1e9  # Convert lamports to SOL
        except Exception as e:
            print(f"Error getting SOL balance: {e}")
            return 0.0

    def get_token_accounts(self, public_key: str) -> list:
        """Get token accounts for a wallet"""
        try:
            from solana.rpc.types import TokenAccountOpts
            
            pub_key = Pubkey.from_string(public_key)
            token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            
            response = self.rpc_client.get_token_accounts_by_owner_json_parsed(
                pub_key,
                TokenAccountOpts(program_id=token_program)
            )
            
            # Handle response object
            accounts = response.value if hasattr(response, 'value') else response
            
            token_list = []
            if accounts:
                for account in accounts:
                    try:
                        # Handle both object and dict responses
                        if hasattr(account, 'account'):
                            account_data = account.account
                        elif isinstance(account, dict) and 'account' in account:
                            account_data = account['account']
                        else:
                            continue
                        
                        # Extract parsed data
                        if hasattr(account_data, 'data'):
                            data = account_data.data
                        elif isinstance(account_data, dict):
                            data = account_data.get('data', {})
                        else:
                            continue
                        
                        # Get parsed info
                        if hasattr(data, 'parsed'):
                            parsed = data.parsed
                        elif isinstance(data, dict):
                            parsed = data.get('parsed', {})
                        else:
                            continue
                        
                        # Extract token info
                        if isinstance(parsed, dict):
                            info = parsed.get('info', {})
                            token_amount = info.get('tokenAmount', {})
                            token_list.append({
                                'mint': info.get('mint', ''),
                                'amount': float(token_amount.get('uiAmount', 0) or 0),
                                'decimals': token_amount.get('decimals', 0)
                            })
                    except Exception as e:
                        print(f"Error parsing token account: {e}")
                        continue
            
            return token_list
        except Exception as e:
            print(f"Error getting token accounts: {e}")
            return []

    def get_transaction_history(self, public_key: str, limit: int = 10) -> list:
        """Get simplified transaction history"""
        try:
            pub_key = Pubkey.from_string(public_key)
            
            # Get signatures
            signatures_response = self.rpc_client.get_signatures_for_address(pub_key, limit=limit)
            sig_list = signatures_response.value if hasattr(signatures_response, 'value') else signatures_response
            
            transactions = []
            for sig_info in sig_list[:limit]:  # Limit to avoid rate limiting
                try:
                    # Extract signature
                    signature = sig_info.signature if hasattr(sig_info, 'signature') else sig_info.get('signature')
                    block_time = sig_info.block_time if hasattr(sig_info, 'block_time') else sig_info.get('blockTime', 0)
                    
                    # Create simplified transaction record
                    tx_record = {
                        'signature': str(signature),
                        'blockTime': block_time,
                        'slot': sig_info.slot if hasattr(sig_info, 'slot') else sig_info.get('slot', 0),
                        'err': sig_info.err if hasattr(sig_info, 'err') else sig_info.get('err')
                    }
                    
                    transactions.append(tx_record)
                except Exception as e:
                    print(f"Error processing signature: {e}")
                    continue
            
            return transactions
        except Exception as e:
            print(f"Error getting transaction history: {e}")
            return []

# Test function
if __name__ == "__main__":
    RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    api = SolanaAPI(RPC_URL)
    
    test_wallet = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
    
    print(f"Testing wallet: {test_wallet}")
    print(f"Balance: {api.get_sol_balance(test_wallet)} SOL")
    print(f"Tokens: {len(api.get_token_accounts(test_wallet))}")
    print(f"Transactions: {len(api.get_transaction_history(test_wallet))}")

