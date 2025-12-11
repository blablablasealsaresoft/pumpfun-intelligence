"""
Multi-API Integration Service
Aggregates data from multiple cryptocurrency APIs for comprehensive token analysis
"""

import requests
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MultiAPIService:
    """Integrates multiple cryptocurrency APIs for enhanced data accuracy"""
    
    def __init__(self):
        self.coingecko_base = "https://api.coingecko.com/api/v3"
        self.jupiter_base = "https://quote-api.jup.ag/v6"
        self.birdeye_base = "https://public-api.birdeye.so"
        self.coinmarketcap_base = "https://pro-api.coinmarketcap.com/v1"
        self.messari_base = "https://data.messari.io/api/v1"
        
        # Rate limiting
        self.last_request_time = {}
        self.min_request_interval = 1.0  # seconds
        
    def _rate_limit(self, api_name: str):
        """Simple rate limiting"""
        if api_name in self.last_request_time:
            elapsed = time.time() - self.last_request_time[api_name]
            if elapsed < self.min_request_interval:
                time.sleep(self.min_request_interval - elapsed)
        self.last_request_time[api_name] = time.time()
    
    # ========================================================================
    # COINGECKO API (Tier 1 - Free, No Auth)
    # ========================================================================
    
    def get_coingecko_token_data(self, solana_address: str) -> Optional[Dict]:
        """
        Get comprehensive token data from CoinGecko
        
        Returns price, market cap, volume, price changes, etc.
        """
        try:
            self._rate_limit('coingecko')
            
            # CoinGecko uses contract address for Solana tokens
            url = f"{self.coingecko_base}/coins/solana/contract/{solana_address}"
            
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                return {
                    'id': data.get('id'),
                    'symbol': data.get('symbol', '').upper(),
                    'name': data.get('name'),
                    'price_usd': data.get('market_data', {}).get('current_price', {}).get('usd'),
                    'market_cap_usd': data.get('market_data', {}).get('market_cap', {}).get('usd'),
                    'total_volume_usd': data.get('market_data', {}).get('total_volume', {}).get('usd'),
                    'price_change_24h': data.get('market_data', {}).get('price_change_percentage_24h'),
                    'price_change_7d': data.get('market_data', {}).get('price_change_percentage_7d'),
                    'price_change_30d': data.get('market_data', {}).get('price_change_percentage_30d'),
                    'ath_usd': data.get('market_data', {}).get('ath', {}).get('usd'),
                    'atl_usd': data.get('market_data', {}).get('atl', {}).get('usd'),
                    'circulating_supply': data.get('market_data', {}).get('circulating_supply'),
                    'total_supply': data.get('market_data', {}).get('total_supply'),
                    'max_supply': data.get('market_data', {}).get('max_supply'),
                    'fdv_usd': data.get('market_data', {}).get('fully_diluted_valuation', {}).get('usd'),
                    'market_cap_rank': data.get('market_cap_rank'),
                    'coingecko_rank': data.get('coingecko_rank'),
                    'coingecko_score': data.get('coingecko_score'),
                    'liquidity_score': data.get('liquidity_score'),
                    'community_score': data.get('community_score'),
                    'last_updated': data.get('last_updated'),
                    'source': 'coingecko'
                }
            else:
                logger.warning(f"CoinGecko API returned {response.status_code} for {solana_address}")
                return None
                
        except Exception as e:
            logger.error(f"CoinGecko API error: {e}")
            return None
    
    def get_coingecko_trending(self) -> List[Dict]:
        """Get trending coins from CoinGecko"""
        try:
            self._rate_limit('coingecko')
            url = f"{self.coingecko_base}/search/trending"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('coins', [])
            return []
        except Exception as e:
            logger.error(f"CoinGecko trending error: {e}")
            return []
    
    # ========================================================================
    # JUPITER AGGREGATOR API (Tier 1 - Free, No Auth)
    # ========================================================================
    
    def get_jupiter_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50) -> Optional[Dict]:
        """
        Get best swap quote from Jupiter Aggregator
        
        Args:
            input_mint: Input token mint address
            output_mint: Output token mint address
            amount: Amount in smallest unit (lamports for SOL)
            slippage_bps: Slippage tolerance in basis points (50 = 0.5%)
        
        Returns quote with best route, price impact, fees
        """
        try:
            self._rate_limit('jupiter')
            
            url = f"{self.jupiter_base}/quote"
            params = {
                'inputMint': input_mint,
                'outputMint': output_mint,
                'amount': amount,
                'slippageBps': slippage_bps
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                return {
                    'input_mint': data.get('inputMint'),
                    'output_mint': data.get('outputMint'),
                    'in_amount': data.get('inAmount'),
                    'out_amount': data.get('outAmount'),
                    'other_amount_threshold': data.get('otherAmountThreshold'),
                    'swap_mode': data.get('swapMode'),
                    'slippage_bps': data.get('slippageBps'),
                    'price_impact_pct': data.get('priceImpactPct'),
                    'route_plan': data.get('routePlan'),
                    'context_slot': data.get('contextSlot'),
                    'time_taken': data.get('timeTaken'),
                    'source': 'jupiter'
                }
            else:
                logger.warning(f"Jupiter API returned {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Jupiter API error: {e}")
            return None
    
    def get_jupiter_price(self, token_address: str, vs_token: str = "So11111111111111111111111111111111111111112") -> Optional[float]:
        """
        Get token price via Jupiter (vs SOL by default)
        
        Args:
            token_address: Token mint address
            vs_token: Quote token (default: SOL)
        
        Returns price as float
        """
        try:
            # Use 1 SOL (1e9 lamports) as amount
            quote = self.get_jupiter_quote(vs_token, token_address, 1000000000)
            
            if quote and quote.get('out_amount'):
                # Calculate price
                in_amount = float(quote['in_amount'])
                out_amount = float(quote['out_amount'])
                price = in_amount / out_amount if out_amount > 0 else 0
                return price
            
            return None
        except Exception as e:
            logger.error(f"Jupiter price error: {e}")
            return None
    
    # ========================================================================
    # BIRDEYE API (Tier 1 - Requires API Key)
    # ========================================================================
    
    def get_birdeye_token_security(self, token_address: str, api_key: Optional[str] = None) -> Optional[Dict]:
        """
        Get token security analysis from Birdeye
        
        Requires API key (free tier available at birdeye.so)
        """
        if not api_key:
            logger.warning("Birdeye API key not provided")
            return None
        
        try:
            self._rate_limit('birdeye')
            
            url = f"{self.birdeye_base}/defi/token_security"
            headers = {
                'X-API-KEY': api_key
            }
            params = {
                'address': token_address
            }
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                security_data = data.get('data', {})
                
                return {
                    'is_token_2022': security_data.get('isToken2022'),
                    'is_mutable': security_data.get('isMutable'),
                    'is_freeze_authority_enabled': security_data.get('isFreezeAuthorityEnabled'),
                    'is_mint_authority_enabled': security_data.get('isMintAuthorityEnabled'),
                    'top_10_holder_percent': security_data.get('top10HolderPercent'),
                    'creator_percent': security_data.get('creatorPercent'),
                    'owner_percent': security_data.get('ownerPercent'),
                    'is_true_token': security_data.get('isTrueToken'),
                    'total_supply': security_data.get('totalSupply'),
                    'holder_count': security_data.get('holderCount'),
                    'security_score': self._calculate_security_score(security_data),
                    'source': 'birdeye'
                }
            else:
                logger.warning(f"Birdeye API returned {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Birdeye API error: {e}")
            return None
    
    def _calculate_security_score(self, security_data: Dict) -> int:
        """
        Calculate security score (0-100) based on Birdeye data
        
        Higher score = safer token
        """
        score = 100
        
        # Penalties
        if security_data.get('isMutable'): score -= 15
        if security_data.get('isFreezeAuthorityEnabled'): score -= 20
        if security_data.get('isMintAuthorityEnabled'): score -= 25
        
        top_10 = security_data.get('top10HolderPercent', 0)
        if top_10 > 50: score -= 20
        elif top_10 > 30: score -= 10
        
        creator_pct = security_data.get('creatorPercent', 0)
        if creator_pct > 20: score -= 15
        elif creator_pct > 10: score -= 5
        
        return max(0, score)
    
    def get_birdeye_token_overview(self, token_address: str, api_key: Optional[str] = None) -> Optional[Dict]:
        """Get comprehensive token overview from Birdeye"""
        if not api_key:
            return None
        
        try:
            self._rate_limit('birdeye')
            
            url = f"{self.birdeye_base}/defi/token_overview"
            headers = {'X-API-KEY': api_key}
            params = {'address': token_address}
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('data', {})
            return None
        except Exception as e:
            logger.error(f"Birdeye overview error: {e}")
            return None
    
    # ========================================================================
    # COINMARKETCAP API (Tier 2 - Requires API Key)
    # ========================================================================
    
    def get_coinmarketcap_quote(self, symbol: str, api_key: Optional[str] = None) -> Optional[Dict]:
        """Get latest quote from CoinMarketCap"""
        if not api_key:
            return None
        
        try:
            self._rate_limit('coinmarketcap')
            
            url = f"{self.coinmarketcap_base}/cryptocurrency/quotes/latest"
            headers = {
                'X-CMC_PRO_API_KEY': api_key,
                'Accept': 'application/json'
            }
            params = {'symbol': symbol}
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                token_data = data.get('data', {}).get(symbol, {})
                quote = token_data.get('quote', {}).get('USD', {})
                
                return {
                    'name': token_data.get('name'),
                    'symbol': token_data.get('symbol'),
                    'price_usd': quote.get('price'),
                    'volume_24h': quote.get('volume_24h'),
                    'volume_change_24h': quote.get('volume_change_24h'),
                    'percent_change_1h': quote.get('percent_change_1h'),
                    'percent_change_24h': quote.get('percent_change_24h'),
                    'percent_change_7d': quote.get('percent_change_7d'),
                    'market_cap': quote.get('market_cap'),
                    'market_cap_dominance': quote.get('market_cap_dominance'),
                    'fully_diluted_market_cap': quote.get('fully_diluted_market_cap'),
                    'source': 'coinmarketcap'
                }
            return None
        except Exception as e:
            logger.error(f"CoinMarketCap error: {e}")
            return None
    
    # ========================================================================
    # MESSARI API (Tier 2 - Free for basic endpoints)
    # ========================================================================
    
    def get_messari_metrics(self, symbol: str) -> Optional[Dict]:
        """Get asset metrics from Messari"""
        try:
            self._rate_limit('messari')
            
            url = f"{self.messari_base}/assets/{symbol}/metrics"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                metrics = data.get('data', {})
                market_data = metrics.get('market_data', {})
                
                return {
                    'symbol': metrics.get('symbol'),
                    'name': metrics.get('name'),
                    'price_usd': market_data.get('price_usd'),
                    'volume_last_24_hours': market_data.get('volume_last_24_hours'),
                    'real_volume_last_24_hours': market_data.get('real_volume_last_24_hours'),
                    'percent_change_usd_last_24_hours': market_data.get('percent_change_usd_last_24_hours'),
                    'source': 'messari'
                }
            return None
        except Exception as e:
            logger.error(f"Messari error: {e}")
            return None
    
    # ========================================================================
    # AGGREGATED ANALYSIS
    # ========================================================================
    
    def get_comprehensive_token_data(self, token_address: str, birdeye_api_key: Optional[str] = None) -> Dict:
        """
        Aggregate data from all available APIs for comprehensive analysis
        
        Returns combined data with confidence scores
        """
        result = {
            'token_address': token_address,
            'timestamp': datetime.utcnow().isoformat(),
            'data_sources': [],
            'price_data': {},
            'security_data': {},
            'market_data': {},
            'confidence_score': 0
        }
        
        # CoinGecko data
        cg_data = self.get_coingecko_token_data(token_address)
        if cg_data:
            result['data_sources'].append('coingecko')
            result['price_data']['coingecko'] = {
                'price_usd': cg_data.get('price_usd'),
                'market_cap': cg_data.get('market_cap_usd'),
                'volume_24h': cg_data.get('total_volume_usd')
            }
            result['market_data'].update({
                'symbol': cg_data.get('symbol'),
                'name': cg_data.get('name'),
                'market_cap_rank': cg_data.get('market_cap_rank'),
                'price_change_24h': cg_data.get('price_change_24h')
            })
            result['confidence_score'] += 30
        
        # Jupiter price
        jupiter_price = self.get_jupiter_price(token_address)
        if jupiter_price:
            result['data_sources'].append('jupiter')
            result['price_data']['jupiter'] = {'price_sol': jupiter_price}
            result['confidence_score'] += 20
        
        # Birdeye security
        if birdeye_api_key:
            security = self.get_birdeye_token_security(token_address, birdeye_api_key)
            if security:
                result['data_sources'].append('birdeye')
                result['security_data'] = security
                result['confidence_score'] += 50
        
        # Calculate consensus price
        prices = []
        if cg_data and cg_data.get('price_usd'):
            prices.append(cg_data['price_usd'])
        
        if prices:
            result['consensus_price_usd'] = sum(prices) / len(prices)
        
        return result
    
    def get_token_security_analysis(self, token_address: str, birdeye_api_key: Optional[str] = None) -> Dict:
        """
        Comprehensive security analysis
        
        Returns rug pull probability, risk level, and recommendations
        """
        security = self.get_birdeye_token_security(token_address, birdeye_api_key)
        
        if not security:
            return {
                'available': False,
                'message': 'Security data not available (Birdeye API key required)'
            }
        
        # Calculate rug pull probability
        rug_pull_score = 0
        
        if security.get('is_mint_authority_enabled'):
            rug_pull_score += 40
        if security.get('is_freeze_authority_enabled'):
            rug_pull_score += 30
        if security.get('creator_percent', 0) > 20:
            rug_pull_score += 20
        if security.get('top_10_holder_percent', 0) > 50:
            rug_pull_score += 10
        
        rug_pull_probability = min(100, rug_pull_score) / 100.0
        
        # Risk level
        if rug_pull_probability > 0.7:
            risk_level = "VERY_HIGH"
            recommendation = "AVOID - High rug pull risk"
        elif rug_pull_probability > 0.4:
            risk_level = "HIGH"
            recommendation = "CAUTION - Significant risk factors"
        elif rug_pull_probability > 0.2:
            risk_level = "MEDIUM"
            recommendation = "PROCEED WITH CAUTION - Some risk factors"
        else:
            risk_level = "LOW"
            recommendation = "RELATIVELY SAFE - Low risk factors"
        
        return {
            'available': True,
            'security_score': security.get('security_score', 0),
            'rug_pull_probability': rug_pull_probability,
            'risk_level': risk_level,
            'recommendation': recommendation,
            'risk_factors': {
                'mint_authority_enabled': security.get('is_mint_authority_enabled'),
                'freeze_authority_enabled': security.get('is_freeze_authority_enabled'),
                'creator_percentage': security.get('creator_percent'),
                'top_10_holder_percentage': security.get('top_10_holder_percent')
            },
            'holder_count': security.get('holder_count'),
            'total_supply': security.get('total_supply')
        }


# Global instance
multi_api = MultiAPIService()

