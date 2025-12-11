"""
Clustering Algorithm Service - THE KILLER FEATURE
Detects coordinated smart money activity before tokens pump
This is what makes the platform profitable!
"""

from typing import List, Dict, Any, Set
from datetime import datetime, timedelta
from collections import defaultdict
import database as db

class ClusterDetector:
    def __init__(self):
        self.time_window_minutes = 5  # Detect wallets buying within 5 minutes
        self.min_wallets = 5  # Minimum wallets for a cluster
        self.min_win_rate = 6000  # 60% win rate (stored as 60 * 100)
        self.similarity_threshold = 0.1  # 10% amount similarity
    
    def detect_temporal_clusters(self, hours: int = 1) -> List[Dict[str, Any]]:
        """
        Detect temporal clusters: wallets buying same token within time window
        This is the PRIMARY signal for coordinated smart money activity
        """
        # Get recent transactions
        transactions = db.get_recent_transactions(hours)
        
        if not transactions:
            return []
        
        clusters = []
        
        # Group by token
        token_groups = defaultdict(list)
        for tx in transactions:
            if tx['transaction_type'] == 'buy':
                token_groups[tx['token_address']].append(tx)
        
        # Analyze each token
        for token_address, txs in token_groups.items():
            for tx in txs:
                tx['_parsed_timestamp'] = datetime.fromisoformat(tx['timestamp'])
            
            # Sort by parsed timestamp
            sorted_txs = sorted(txs, key=lambda x: x['_parsed_timestamp'])
            
            # Sliding window analysis
            for i in range(len(sorted_txs)):
                window_start = sorted_txs[i]['_parsed_timestamp']
                window_end = window_start + timedelta(minutes=self.time_window_minutes)
                
                wallets_in_window = set()
                total_volume = 0
                txs_in_window = []
                
                for j in range(i, len(sorted_txs)):
                    tx_time = sorted_txs[j]['_parsed_timestamp']
                    
                    if tx_time <= window_end:
                        wallets_in_window.add(sorted_txs[j]['wallet_address'])
                        total_volume += sorted_txs[j]['amount_usd']
                        txs_in_window.append(sorted_txs[j])
                    else:
                        break
                
                # Check if cluster meets criteria
                if len(wallets_in_window) >= self.min_wallets:
                    # Calculate cluster score
                    score = self._calculate_temporal_score(
                        len(wallets_in_window),
                        total_volume,
                        self.time_window_minutes
                    )
                    
                    # Check if wallets are smart money
                    smart_money_count = self._count_smart_money_wallets(list(wallets_in_window))
                    
                    cluster = {
                        'cluster_type': 'temporal',
                        'token_address': token_address,
                        'wallet_addresses': list(wallets_in_window),
                        'wallet_count': len(wallets_in_window),
                        'smart_money_count': smart_money_count,
                        'total_volume_usd': total_volume,
                        'cluster_score': score,
                        'detected_at': datetime.now(),
                        'window_start': window_start,
                        'window_end': window_end,
                        'signal': 'STRONG_BUY' if score > 7000 else 'BUY' if score > 5000 else 'MONITOR'
                    }
                    
                    clusters.append(cluster)
                    
                    # Skip ahead to avoid overlapping clusters
                    i += len(wallets_in_window) - 1
        
        # Merge overlapping clusters
        merged = self._merge_clusters(clusters)
        
        # Sort by score
        merged.sort(key=lambda x: x['cluster_score'], reverse=True)
        
        return merged
    
    def detect_amount_similarity_clusters(self, hours: int = 1) -> List[Dict[str, Any]]:
        """
        Detect wallets buying similar amounts (coordinated bots/insiders)
        """
        transactions = db.get_recent_transactions(hours)
        
        if not transactions:
            return []
        
        clusters = []
        
        # Group by token
        token_groups = defaultdict(list)
        for tx in transactions:
            if tx['transaction_type'] == 'buy':
                token_groups[tx['token_address']].append(tx)
        
        # Analyze each token
        for token_address, txs in token_groups.items():
            # Group by similar amounts
            amount_groups = []
            
            for tx in txs:
                found_group = False
                
                for group in amount_groups:
                    avg_amount = sum(t['amount_usd'] for t in group) / len(group)
                    difference = abs(tx['amount_usd'] - avg_amount) / avg_amount if avg_amount > 0 else 1
                    
                    if difference <= self.similarity_threshold:
                        group.append(tx)
                        found_group = True
                        break
                
                if not found_group:
                    amount_groups.append([tx])
            
            # Check each group
            for group in amount_groups:
                if len(group) >= 3:  # Lower threshold for amount similarity
                    wallets = set(tx['wallet_address'] for tx in group)
                    total_volume = sum(tx['amount_usd'] for tx in group)
                    
                    score = self._calculate_amount_similarity_score(
                        len(wallets),
                        total_volume,
                        self.similarity_threshold
                    )
                    
                    smart_money_count = self._count_smart_money_wallets(list(wallets))
                    
                    cluster = {
                        'cluster_type': 'smart_money',
                        'token_address': token_address,
                        'wallet_addresses': list(wallets),
                        'wallet_count': len(wallets),
                        'smart_money_count': smart_money_count,
                        'total_volume_usd': total_volume,
                        'cluster_score': score,
                        'detected_at': datetime.now(),
                        'avg_amount': total_volume / len(group),
                        'signal': 'STRONG_BUY' if score > 7000 else 'BUY' if score > 5000 else 'MONITOR'
                    }
                    
                    clusters.append(cluster)
        
        clusters.sort(key=lambda x: x['cluster_score'], reverse=True)
        return clusters
    
    def detect_early_accumulation(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Detect early accumulation: many wallets buying low-volume token
        (before it pumps)
        """
        transactions = db.get_recent_transactions(hours)
        
        if not transactions:
            return []
        
        clusters = []
        
        # Group by token
        token_groups = defaultdict(list)
        for tx in transactions:
            token_groups[tx['token_address']].append(tx)
        
        # Analyze each token
        for token_address, txs in token_groups.items():
            unique_wallets = set(tx['wallet_address'] for tx in txs)
            total_volume = sum(tx['amount_usd'] for tx in txs)
            
            # Early accumulation criteria:
            # - Many unique wallets (10+)
            # - Low total volume (<$100K in 24h)
            # - Indicates accumulation before pump
            
            if len(unique_wallets) >= 10 and total_volume <= 10000000:  # $100K in cents
                score = self._calculate_early_accumulation_score(
                    len(unique_wallets),
                    total_volume,
                    10000000  # max volume threshold
                )
                
                smart_money_count = self._count_smart_money_wallets(list(unique_wallets))
                
                cluster = {
                    'cluster_type': 'early_accumulation',
                    'token_address': token_address,
                    'wallet_addresses': list(unique_wallets),
                    'wallet_count': len(unique_wallets),
                    'smart_money_count': smart_money_count,
                    'total_volume_usd': total_volume,
                    'cluster_score': score,
                    'detected_at': datetime.now(),
                    'signal': 'ACCUMULATE' if score > 6000 else 'MONITOR'
                }
                
                clusters.append(cluster)
        
        clusters.sort(key=lambda x: x['cluster_score'], reverse=True)
        return clusters
    
    def _calculate_temporal_score(self, wallet_count: int, total_volume: int, time_window: int) -> int:
        """
        Calculate cluster score for temporal clusters
        Score range: 0-10000 (0-100%)
        """
        # Wallet count score (max 5000)
        wallet_score = min(wallet_count * 100, 5000)
        
        # Volume score (max 3000)
        volume_score = min((total_volume / 100000) * 1000, 3000)
        
        # Time concentration score (max 2000)
        # Shorter time window = higher score
        time_score = max(2000 - time_window * 100, 0)
        
        total_score = wallet_score + volume_score + time_score
        return min(int(total_score), 10000)
    
    def _calculate_amount_similarity_score(self, wallet_count: int, total_volume: int, similarity: float) -> int:
        """
        Calculate score for amount similarity clusters
        """
        wallet_score = min(wallet_count * 150, 5000)
        volume_score = min((total_volume / 100000) * 1000, 3000)
        similarity_score = min((1 - similarity) * 2000, 2000)
        
        total_score = wallet_score + volume_score + similarity_score
        return min(int(total_score), 10000)
    
    def _calculate_early_accumulation_score(self, wallet_count: int, total_volume: int, max_volume: int) -> int:
        """
        Calculate score for early accumulation
        """
        wallet_score = min(wallet_count * 200, 6000)
        
        volume_ratio = total_volume / max_volume if max_volume > 0 else 0
        volume_score = min((1 - volume_ratio) * 4000, 4000)
        
        total_score = wallet_score + volume_score
        return min(int(total_score), 10000)
    
    def _count_smart_money_wallets(self, wallet_addresses: List[str]) -> int:
        """
        Count how many wallets in the cluster are smart money (high win rate)
        """
        count = 0
        for address in wallet_addresses:
            wallet = db.get_wallet(address)
            if wallet and wallet['win_rate'] >= self.min_win_rate:
                count += 1
        return count
    
    def _merge_clusters(self, clusters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Merge overlapping clusters for the same token
        """
        if not clusters:
            return []
        
        merged = []
        
        for cluster in clusters:
            found_merge = False
            
            for existing in merged:
                if existing['token_address'] == cluster['token_address']:
                    # Check wallet overlap
                    overlap = set(cluster['wallet_addresses']) & set(existing['wallet_addresses'])
                    
                    if len(overlap) > len(cluster['wallet_addresses']) * 0.5:
                        # Merge clusters
                        all_wallets = set(existing['wallet_addresses']) | set(cluster['wallet_addresses'])
                        existing['wallet_addresses'] = list(all_wallets)
                        existing['wallet_count'] = len(all_wallets)
                        existing['total_volume_usd'] += cluster['total_volume_usd']
                        existing['cluster_score'] = max(existing['cluster_score'], cluster['cluster_score'])
                        found_merge = True
                        break
            
            if not found_merge:
                merged.append(cluster)
        
        return merged
    
    def save_cluster_to_db(self, cluster: Dict[str, Any]) -> int:
        """
        Save detected cluster to database
        Returns cluster ID
        """
        cluster_id = db.insert_cluster(
            token_address=cluster['token_address'],
            cluster_type=cluster['cluster_type'],
            wallet_count=cluster['wallet_count'],
            total_volume_usd=cluster['total_volume_usd'],
            cluster_score=cluster['cluster_score']
        )
        
        # Save cluster wallets
        for wallet_address in cluster['wallet_addresses']:
            db.insert_cluster_wallet(cluster_id, wallet_address)
        
        return cluster_id
    
    def detect_all_clusters(self, hours: int = 1) -> Dict[str, List[Dict[str, Any]]]:
        """
        Run all cluster detection algorithms
        Returns dict with all cluster types
        """
        return {
            'temporal': self.detect_temporal_clusters(hours),
            'amount_similarity': self.detect_amount_similarity_clusters(hours),
            'early_accumulation': self.detect_early_accumulation(24)  # Always check 24h for accumulation
        }

# Global instance
cluster_detector = ClusterDetector()

