"""
DexScreener API Integration
Provides real-time token data, liquidity, volume, and price information
"""

import requests
from typing import Optional, List, Dict, Any
from datetime import datetime

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"

class DexScreenerAPI:
    def __init__(self):
        self.base_url = DEXSCREENER_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'PumpFun-Intelligence/1.0'
        })
    
    def get_token_pairs(self, token_address: str) -> List[Dict[str, Any]]:
        """
        Get all trading pairs for a token
        Returns list of pairs with price, liquidity, volume data
        """
        try:
            url = f"{self.base_url}/latest/dex/tokens/{token_address}"
            response = self.session.get(url, timeout=10)
            
            if response.status_code != 200:
                print(f"[DexScreener] API error: {response.status_code}")
                return []
            
            data = response.json()
            return data.get('pairs', [])
        
        except Exception as e:
            print(f"[DexScreener] Error fetching token pairs: {e}")
            return []

    def get_latest_pairs(self, chain: str = "solana", limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get latest pairs for a chain.
        """
        try:
            url = f"{self.base_url}/latest/dex/pairs/{chain}"
            response = self.session.get(url, timeout=10)
            if response.status_code != 200:
                print(f"[DexScreener] API error: {response.status_code}")
                return []
            data = response.json()
            pairs = data.get("pairs", [])[:limit]
            return pairs
        except Exception as e:
            print(f"[DexScreener] Error fetching latest pairs: {e}")
            return []
    
    def get_pair_data(self, chain: str, pair_address: str) -> Optional[Dict[str, Any]]:
        """
        Get specific pair data
        """
        try:
            url = f"{self.base_url}/latest/dex/pairs/{chain}/{pair_address}"
            response = self.session.get(url, timeout=10)
            
            if response.status_code != 200:
                print(f"[DexScreener] API error: {response.status_code}")
                return None
            
            data = response.json()
            return data.get('pair')
        
        except Exception as e:
            print(f"[DexScreener] Error fetching pair data: {e}")
            return None
    
    def search_tokens(self, query: str) -> List[Dict[str, Any]]:
        """
        Search for tokens by name or symbol
        """
        try:
            url = f"{self.base_url}/latest/dex/search?q={requests.utils.quote(query)}"
            response = self.session.get(url, timeout=10)
            
            if response.status_code != 200:
                print(f"[DexScreener] API error: {response.status_code}")
                return []
            
            data = response.json()
            return data.get('pairs', [])
        
        except Exception as e:
            print(f"[DexScreener] Error searching tokens: {e}")
            return []
    
    def get_token_data(self, chain: str, token_address: str) -> Optional[Dict[str, Any]]:
        """
        Get comprehensive token data including all pairs
        Returns aggregated data across all pairs
        """
        pairs = self.get_token_pairs(token_address)
        
        if not pairs:
            return None
        
        # Filter for the specified chain
        chain_pairs = [p for p in pairs if p.get('chainId', '').lower() == chain.lower()]
        
        if not chain_pairs:
            chain_pairs = pairs  # Fallback to all pairs
        
        primary_pair = chain_pairs[0]
        
        # Aggregate data across all pairs
        total_volume_24h = sum(p.get('volume', {}).get('h24', 0) for p in chain_pairs)
        total_liquidity = sum(p.get('liquidity', {}).get('usd', 0) for p in chain_pairs)
        
        # Estimate unique wallets from transaction counts
        unique_wallets_24h = self.estimate_unique_wallets(primary_pair)
        
        return {
            'address': token_address,
            'chain': chain,
            'symbol': primary_pair.get('baseToken', {}).get('symbol', ''),
            'name': primary_pair.get('baseToken', {}).get('name', ''),
            'price_usd': float(primary_pair.get('priceUsd', 0)),
            'liquidity_usd': total_liquidity,
            'volume_24h': total_volume_24h,
            'unique_wallets_24h': unique_wallets_24h,
            'price_change_24h': primary_pair.get('priceChange', {}).get('h24', 0),
            'price_change_1h': primary_pair.get('priceChange', {}).get('h1', 0),
            'price_change_5m': primary_pair.get('priceChange', {}).get('m5', 0),
            'fdv': primary_pair.get('fdv'),
            'market_cap': primary_pair.get('marketCap'),
            'pair_created_at': primary_pair.get('pairCreatedAt'),
            'pairs': chain_pairs
        }
    
    def estimate_unique_wallets(self, pair: Dict[str, Any]) -> int:
        """
        Estimate unique wallets from transaction counts
        """
        txns = pair.get('txns', {})
        h24 = txns.get('h24', {})
        
        buys = h24.get('buys', 0)
        sells = h24.get('sells', 0)
        
        # Rough estimate: total transactions
        return buys + sells
    
    def check_graduation_status(self, token_address: str) -> Dict[str, Any]:
        """
        Check if token has graduated from Pump.fun to Raydium
        Returns graduation status and timing
        """
        pairs = self.get_token_pairs(token_address)
        
        if not pairs:
            return {
                'graduated': False,
                'reason': 'No pairs found'
            }
        
        # Check for Raydium pairs
        raydium_pairs = [p for p in pairs if 'raydium' in p.get('dexId', '').lower()]
        
        if raydium_pairs:
            # Token has graduated to Raydium
            pair = raydium_pairs[0]
            created_at = pair.get('pairCreatedAt')
            
            graduation_time = None
            if created_at:
                graduation_time = datetime.fromtimestamp(created_at / 1000)
            
            return {
                'graduated': True,
                'graduation_time': graduation_time,
                'dex': 'Raydium',
                'pair_address': pair.get('pairAddress'),
                'liquidity_usd': pair.get('liquidity', {}).get('usd', 0),
                'volume_24h': pair.get('volume', {}).get('h24', 0),
                'price_usd': float(pair.get('priceUsd', 0)),
                'signal': 'STRONG_BUY' if pair.get('volume', {}).get('h1', 0) > 10000 else 'MONITOR'
            }
        
        return {
            'graduated': False,
            'reason': 'Still on Pump.fun',
            'signal': 'WAIT'
        }
    
    def analyze_liquidity_changes(self, token_address: str) -> Dict[str, Any]:
        """
        Analyze liquidity changes (rug pull indicator)
        """
        pairs = self.get_token_pairs(token_address)
        
        if not pairs:
            return {
                'risk': 'UNKNOWN',
                'reason': 'No data available'
            }
        
        primary_pair = pairs[0]
        liquidity = primary_pair.get('liquidity', {}).get('usd', 0)
        volume_24h = primary_pair.get('volume', {}).get('h24', 0)
        
        # Risk indicators
        if liquidity < 1000:
            return {
                'risk': 'CRITICAL',
                'reason': 'Very low liquidity (<$1K)',
                'liquidity_usd': liquidity,
                'recommendation': 'DO_NOT_BUY'
            }
        
        if liquidity < 5000:
            return {
                'risk': 'HIGH',
                'reason': 'Low liquidity (<$5K)',
                'liquidity_usd': liquidity,
                'recommendation': 'EXTREME_CAUTION'
            }
        
        # Check volume/liquidity ratio
        if volume_24h > 0:
            ratio = volume_24h / liquidity
            if ratio > 10:
                return {
                    'risk': 'MEDIUM',
                    'reason': 'High volume/liquidity ratio (potential volatility)',
                    'liquidity_usd': liquidity,
                    'volume_24h': volume_24h,
                    'recommendation': 'MONITOR_CLOSELY'
                }
        
        return {
            'risk': 'LOW',
            'reason': 'Healthy liquidity',
            'liquidity_usd': liquidity,
            'volume_24h': volume_24h,
            'recommendation': 'SAFE_TO_TRADE'
        }
    
    def get_holder_distribution(self, token_address: str) -> Dict[str, Any]:
        """
        Analyze holder distribution from transaction patterns
        """
        pairs = self.get_token_pairs(token_address)
        
        if not pairs:
            return {
                'analysis': 'No data available'
            }
        
        primary_pair = pairs[0]
        txns = primary_pair.get('txns', {})
        
        # Analyze transaction patterns
        h24 = txns.get('h24', {})
        h1 = txns.get('h1', {})
        m5 = txns.get('m5', {})
        
        total_24h = h24.get('buys', 0) + h24.get('sells', 0)
        total_1h = h1.get('buys', 0) + h1.get('sells', 0)
        total_5m = m5.get('buys', 0) + m5.get('sells', 0)
        
        # Detect concentration
        if total_5m > total_1h * 0.5:
            concentration = 'HIGH'
            analysis = 'Concentrated activity in last 5 minutes (potential coordinated action)'
        elif total_1h > total_24h * 0.3:
            concentration = 'MEDIUM'
            analysis = 'Increased activity in last hour'
        else:
            concentration = 'LOW'
            analysis = 'Distributed activity over 24 hours'
        
        return {
            'concentration': concentration,
            'analysis': analysis,
            'txns_24h': total_24h,
            'txns_1h': total_1h,
            'txns_5m': total_5m,
            'smart_money_present': concentration == 'HIGH'
        }

# Global instance
dexscreener = DexScreenerAPI()

