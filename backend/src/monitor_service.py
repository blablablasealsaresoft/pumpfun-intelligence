"""
Real-Time Monitoring Service
Continuously scans for smart money clusters and sends instant Telegram alerts
This is what makes the platform profitable in real-time!
"""

import time
import os
import threading
from datetime import datetime, timedelta
from typing import Set, Dict, Any
import database as db
from dexscreener_api import dexscreener
from clustering_service import cluster_detector
from telegram_service import telegram_bot
from executor import trade_executor
from geyser_watcher import GeyserWatcher
from snipe_executor import SnipeExecutor
from trading import metrics_collector
from kol_watcher import KOLWatcher
from kol_sniper import KOLSniper
from bundle_detector import BundleDetector
from bundle_sniper import BundleSniper
from solana.rpc.async_api import AsyncClient

class MonitoringService:
    def __init__(self):
        self.scan_interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))  # Scan every 60 seconds (maximum frequency)
        # Cluster scores are stored as 0-10000 (percentage * 100)
        self.min_cluster_score = int(float(os.getenv("MIN_CLUSTER_SCORE", "70")) * 100)
        self.alerted_clusters: Set[str] = set()  # Track alerted clusters to avoid spam
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
        self.running = False
        self.auto_trade_enabled = trade_executor.enabled
        # New pool watch
        self.enable_new_pool_watch = os.getenv("ENABLE_NEW_POOL_WATCH", "false").lower() in {"1", "true", "yes", "on"}
        self.new_pool_interval = int(os.getenv("NEW_POOL_INTERVAL_SECONDS", "30"))
        self.new_pool_max_age_min = float(os.getenv("NEW_POOL_MAX_AGE_MINUTES", "5"))
        self.new_pool_min_liq = float(os.getenv("NEW_POOL_MIN_LIQ_USD", "5000"))
        self._seen_new_pools: Set[str] = set()
        # Snipe system (Item #6)
        self.snipe_executor = SnipeExecutor(trade_executor)
        self.geyser_watcher = GeyserWatcher(on_new_pool=self.snipe_executor.handle_new_pool)
        # Cluster filters/config
        self.lp_whitelist = set(w.strip() for w in os.getenv("LP_WHITELIST", "").split(",") if w.strip())
        self.min_sol_liq = float(os.getenv("MIN_SOL_LIQ", "0"))
        self.min_base_decimals = int(os.getenv("MIN_BASE_DECIMALS", "6"))
        self.max_base_decimals = int(os.getenv("MAX_BASE_DECIMALS", "9"))
        self.sm_overlap_threshold = float(os.getenv("SMART_MONEY_OVERLAP_THRESHOLD", "0.2"))
        self.sm_overlap_boost = int(os.getenv("SMART_MONEY_OVERLAP_BOOST", "500"))
        self.safety_penalty_enabled = os.getenv("SAFETY_PENALTY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        self.safety_penalty_points = int(os.getenv("SAFETY_PENALTY_POINTS", "300"))
        self.ml_blend_weight = float(os.getenv("ML_BLEND_WEIGHT", "0.0"))
        coeffs_str = os.getenv("ML_COEFFS", "")
        self.ml_coeffs = [float(c) for c in coeffs_str.split(",") if c.strip()] if coeffs_str else []
        try:
            self.ml_intercept = float(os.getenv("ML_INTERCEPT", "0"))
        except Exception:
            self.ml_intercept = 0.0
        self.cadence_window_min = float(os.getenv("CADENCE_WINDOW_MIN", "15"))
        self.cadence_min_repeats = int(os.getenv("CADENCE_MIN_REPEATS", "2"))
        self.cadence_boost_points = int(os.getenv("CADENCE_BOOST_POINTS", "300"))
        # Caches for deltas
        self._liq_history = {}  # token -> list of (ts, liq_usd, liq_sol)
        self._holder_history = {}  # token -> (ts, holder_count, unique_24h)
        self._smart_seen = {}  # token -> wallet -> last_ts
        # KOL sniping
        self.kol_watch_enabled = os.getenv("ENABLE_KOL_SNIPE", "false").lower() in {"1", "true", "yes", "on"}
        self.kol_watcher = None
        self.kol_sniper = None
        # Bundle detection/sniping
        self.bundle_enabled = os.getenv("ENABLE_BUNDLE_SNIPE", "false").lower() in {"1", "true", "yes", "on"}
        self.bundle_detector = None
        self.bundle_sniper = None
        
        print(f"[Monitor] Initialized with {self.scan_interval}s scan interval")
        print(f"[Monitor] Telegram alerts: {'Enabled' if self.telegram_chat_id else 'Disabled (set TELEGRAM_CHAT_ID)'}")
        print(f"[Monitor] Auto-trade: {'Enabled' if self.auto_trade_enabled else 'Disabled or dry-run'}")
        print(f"[Monitor] New pool watch: {'Enabled' if self.enable_new_pool_watch else 'Disabled'}")
        self.rug_price_drop_pct = float(os.getenv("RUG_PRICE_DROP_PCT", "35"))
        self.rug_liq_threshold_usd = float(os.getenv("RUG_LIQ_THRESHOLD_USD", "2000"))
        # Cache for bundled launches -> auto-snipe
        self.auto_snipe_on_pool = os.getenv("AUTO_SNIPE_ON_POOL", "true").lower() in {"1", "true", "yes", "on"}
    
    def start(self):
        """Start the monitoring service"""
        self.running = True
        print("=" * 60)
        print("🚀 MONITORING SERVICE STARTED")
        print("=" * 60)
        print(f"⏱️  Scan Interval: {self.scan_interval} seconds")
        print(f"📊 Min Cluster Score: {self.min_cluster_score / 100}/100")
        print(f"📱 Telegram Alerts: {'ON' if self.telegram_chat_id else 'OFF'}")
        print("=" * 60)
        
        scan_count = 0

        # Start new pool watcher thread if enabled
        if self.enable_new_pool_watch:
            t = threading.Thread(target=self._watch_new_pools, daemon=True)
            t.start()
        # Start flatten watcher
        t_flatten = threading.Thread(target=self._watch_flatten, daemon=True)
        t_flatten.start()
        # Start Telegram command watcher
        t_commands = threading.Thread(target=self._watch_telegram_commands, daemon=True)
        t_commands.start()
        # Start logs subscribe watcher (Raydium/Orca) if enabled
        if os.getenv("ENABLE_LOGS_WATCH", "false").lower() in {"1", "true", "yes", "on"}:
            t_logs = threading.Thread(target=self._watch_program_logs, daemon=True)
            t_logs.start()
        # Start Geyser (pending/slot) watcher if configured
        if os.getenv("ENABLE_GEYSER_WATCH", "false").lower() in {"1", "true", "yes", "on"}:
            t_geyser = threading.Thread(target=self._start_geyser_watch, daemon=True)
            t_geyser.start()
        # Start KOL watcher/sniper if enabled
        if self.kol_watch_enabled:
            try:
                self.kol_sniper = KOLSniper(trade_executor)
                wallets = self._load_kol_wallets()
                self.kol_watcher = KOLWatcher(kol_wallets=wallets, on_kol_buy=self.kol_sniper.handle_kol_buy)
                t_kol = threading.Thread(target=self._start_kol_watch, daemon=True)
                t_kol.start()
                print(f"[Monitor] KOL watch started for {len(wallets)} wallets")
            except Exception as e:
                print(f"[Monitor] Failed to start KOL watcher: {e}")
        # Start bundle detector/sniper if enabled
        if self.bundle_enabled:
            try:
                if not self.kol_sniper:
                    self.kol_sniper = KOLSniper(trade_executor)
                self.bundle_sniper = BundleSniper(self.kol_sniper)
                self.bundle_detector = BundleDetector(on_launch_detected=self.bundle_sniper.handle_launch)
                t_bundle = threading.Thread(target=self._start_bundle_watch, daemon=True)
                t_bundle.start()
                print("[Monitor] Bundle detector started")
            except Exception as e:
                print(f"[Monitor] Failed to start bundle detector: {e}")
        
        while self.running:
            try:
                scan_count += 1
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan #{scan_count} starting...")
                
                # Run cluster detection
                self._scan_for_clusters()
                
                # Check for token graduations
                self._check_graduations()
                
                # Monitor tracked wallets (if any)
                self._monitor_tracked_wallets()
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan #{scan_count} complete. Waiting {self.scan_interval}s...")
                
                # Wait for next scan
                time.sleep(self.scan_interval)
            
            except KeyboardInterrupt:
                print("\n[Monitor] Stopping...")
                self.running = False
                break
            
            except Exception as e:
                print(f"[Monitor] Error in scan: {e}")
                time.sleep(self.scan_interval)
    
    def _scan_for_clusters(self):
        """Scan for new smart money clusters"""
        try:
            # Detect all cluster types
            all_clusters = cluster_detector.detect_all_clusters(hours=1)
            
            total_found = sum(len(clusters) for clusters in all_clusters.values())
            print(f"  📊 Found {total_found} total clusters")
            
            # Process each cluster type
            for cluster_type, clusters in all_clusters.items():
                for cluster in clusters:
                    try:
                        metrics_collector.record_cluster_detected(cluster_type, int(cluster.get("cluster_score", 0)))
                    except Exception:
                        pass
                    if cluster['cluster_score'] >= self.min_cluster_score:
                        self._process_cluster(cluster)
            
        except Exception as e:
            print(f"  ❌ Cluster scan error: {e}")
    
    def _process_cluster(self, cluster: Dict[str, Any]):
        """Process a detected cluster"""
        try:
            # Create unique cluster ID
            cluster_id = f"{cluster['token_address']}_{cluster['cluster_type']}_{cluster['detected_at'].timestamp()}"
            
            # Check if already alerted
            if cluster_id in self.alerted_clusters:
                return
            
            score = cluster['cluster_score'] / 100
            print(f"  🎯 HIGH-SCORE CLUSTER: {cluster['cluster_type']} | Score: {score:.1f}/100 | Wallets: {cluster['wallet_count']}")
            
            # Save to database
            db_cluster_id = cluster_detector.save_cluster_to_db(cluster)
            print(f"     💾 Saved to database (ID: {db_cluster_id})")
            
            # Get token data from DexScreener
            token_data = dexscreener.get_token_data('solana', cluster['token_address'])
            
            if token_data:
                print(f"     📈 Token: ${token_data['symbol']} | Price: ${token_data['price_usd']:.8f}")
                self._maybe_panic_exit(token_data)
                # Enrich cluster with liquidity/pool age
                liq_usd = float(token_data.get("liquidity_usd", 0) or 0)
                pool_age_min = None
                created_at = token_data.get("pair_created_at") or 0
                if created_at:
                    pool_age_min = max(0, (time.time() - (created_at / 1000)) / 60)
                    cluster["pool_age_minutes"] = pool_age_min
                cluster["liquidity_usd"] = liq_usd
                # Best-effort SOL-side liquidity if available
                liq_sol = token_data.get("liquidity_sol") or token_data.get("liq_sol") or None
                if liq_sol is not None:
                    try:
                        cluster["liquidity_sol"] = float(liq_sol)
                    except Exception:
                        pass
                # Raydium pool depth via direct fetch (best-effort)
                pool_depth_sol, pool_depth_usd = self._raydium_depth(token_data.get("address") or cluster["token_address"])
                if pool_depth_sol is not None:
                    cluster["pool_depth_sol"] = pool_depth_sol
                if pool_depth_usd is not None:
                    cluster["pool_depth_usd"] = pool_depth_usd
                # Liquidity deltas (5m/30m)
                deltas = self._update_liquidity_deltas(cluster["token_address"], liq_usd, cluster.get("liquidity_sol"))
                cluster.update(deltas)
                # Holder / unique growth (best-effort from token_data)
                holder_growth = self._update_holder_growth(
                    cluster["token_address"],
                    token_data.get("holder_count"),
                    token_data.get("unique_wallets_24h"),
                )
                cluster.update(holder_growth)
                metrics_collector.update_cluster_last(
                    cluster_type=cluster["cluster_type"],
                    score=cluster.get("cluster_score"),
                    liq_usd=liq_usd,
                    liq_sol=cluster.get("liquidity_sol"),
                    pool_age_min=cluster.get("pool_age_minutes"),
                )
                metrics_collector.update_cluster_deltas(
                    cluster_type=cluster["cluster_type"],
                    liq_delta_5m_usd=cluster.get("liq_delta_5m_usd"),
                    liq_delta_30m_usd=cluster.get("liq_delta_30m_usd"),
                    holder_growth_24h=cluster.get("holder_growth_24h"),
                    unique_wallets_24h_delta=cluster.get("unique_wallets_24h_delta"),
                )

            # Apply filters and boosts
            if not self._cluster_passes_filters(cluster, token_data):
                return
            self._apply_cluster_boosts(cluster, token_data)
            
            # Send Telegram alert
            if self.telegram_chat_id and telegram_bot.enabled:
                success = telegram_bot.send_cluster_alert(
                    self.telegram_chat_id,
                    cluster,
                    token_data
                )
                
                if success:
                    print(f"     📱 Telegram alert sent!")
                    self.alerted_clusters.add(cluster_id)
                else:
                    print(f"     ⚠️  Telegram alert failed")

            # Auto-trade (safe by default with dry-run in executor)
            if token_data and trade_executor.should_trade(cluster, token_data):
                trade_result = trade_executor.execute_buy(cluster, token_data)
                print(f"     🤖 Auto-trade result: {trade_result.get('status')}")
                try:
                    metrics_collector.record_cluster_autotrade(
                        result=trade_result.get("status", "unknown"),
                        reason=trade_result.get("reason", "none"),
                    )
                except Exception:
                    pass
                # If this is a synthetic pool/new_pool cluster, optionally trigger snipe
                if self.auto_snipe_on_pool and cluster.get("cluster_type") in {"new_pool", "pool_create"}:
                    try:
                        self.snipe_executor._loop = trade_executor._loop  # reuse loop if needed
                        # Snipe best-effort; small size is governed by snipe config
                        asyncio.run_coroutine_threadsafe(
                            self.snipe_executor.handle_new_pool(self._cluster_to_pool_event(cluster, token_data)),
                            trade_executor._loop,
                        )
                    except Exception as e:
                        print(f"[Monitor] Auto-snipe trigger error: {e}")
            
            # Clean old alerted clusters (keep last 1000)
            if len(self.alerted_clusters) > 1000:
                self.alerted_clusters = set(list(self.alerted_clusters)[-1000:])
        
        except Exception as e:
            print(f"  ❌ Error processing cluster: {e}")

    def _watch_flatten(self):
        """Watch for flatten flag and trigger sell-all."""
        while self.running:
            try:
                flatten_file = os.getenv("FLATTEN_FILE", "flatten.flag")
                if flatten_file and os.path.exists(flatten_file):
                    print("[Monitor] Flatten flag detected. Selling all positions.")
                    trade_executor.flatten_positions()
                    os.remove(flatten_file)
                time.sleep(2)
            except Exception as e:
                print(f"[Monitor] Flatten watch error: {e}")
                time.sleep(2)

    def _watch_new_pools(self):
        """Poll DexScreener latest pairs for fresh pools and auto-snipe if gates pass."""
        while self.running:
            try:
                pairs = dexscreener.get_latest_pairs(chain="solana", limit=50)
                now_ms = time.time() * 1000
                for pair in pairs:
                    addr = pair.get("baseToken", {}).get("address") or pair.get("pairAddress")
                    if not addr:
                        continue
                    if addr in self._seen_new_pools:
                        continue
                    created_at = pair.get("pairCreatedAt") or 0
                    age_min = (now_ms - created_at) / 1000 / 60 if created_at else 999
                    liq = pair.get("liquidity", {}).get("usd", 0) or 0
                    if created_at and age_min <= self.new_pool_max_age_min and liq >= self.new_pool_min_liq:
                        token_data = {
                            "address": addr,
                            "liquidity_usd": liq,
                            "price_usd": float(pair.get("priceUsd", 0) or 0),
                            "pair_created_at": created_at,
                            "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
                        }
                            self._maybe_panic_exit(token_data)
                        # Synthetic high-score cluster to reuse pipeline
                        cluster = {
                            "cluster_type": "new_pool",
                            "token_address": addr,
                            "wallet_addresses": [],
                            "wallet_count": 0,
                            "smart_money_count": 0,
                            "total_volume_usd": pair.get("volume", {}).get("h24", 0),
                            "cluster_score": 10000,
                            "detected_at": datetime.now(),
                            "signal": "STRONG_BUY",
                        }
                        # Alert
                        if self.telegram_chat_id and telegram_bot.enabled:
                            telegram_bot.send_cluster_alert(self.telegram_chat_id, cluster, token_data)
                        # Auto-trade
                        if trade_executor.should_trade(cluster, token_data):
                            trade_executor.execute_buy(cluster, token_data)
                        self._seen_new_pools.add(addr)
                time.sleep(self.new_pool_interval)
            except Exception as e:
                print(f"[Monitor] New pool watch error: {e}")
                time.sleep(self.new_pool_interval)

    def _watch_program_logs(self):
        """
        Lightweight logsSubscribe to Raydium/Orca programs; on any log, trigger a fast latest-pairs scan.
        """
        import websocket
        import json as _json
        import base58
        import re

        ws_url = os.getenv("SOLANA_WS_URL") or os.getenv("SOLANA_RPC_URL", "").replace("https", "wss")
        if not ws_url:
            print("[Monitor] Logs watch skipped: no SOLANA_WS_URL")
            return
        raydium_prog = os.getenv("RAYDIUM_PROGRAM_ID", "RVKd61ztZW9dqrjK5vCZH1vZ1tc665Ar72Xd1LgjAoG")
        orca_prog = os.getenv("ORCA_PROGRAM_ID", "9WwN7dBDEuDfSUdifYEYdzSsfXCMVvjJhtCmvYzuq76A")
        pumpfun_prog = os.getenv("PUMPFUN_PROGRAM_ID", "pump111111111111111111111111111111111111111")
        filter_mentions = [raydium_prog, orca_prog, pumpfun_prog]
        raydium_ix_hashes = set(h.strip() for h in os.getenv("RAYDIUM_CREATE_IX_HASHES", "").split(",") if h.strip())
        orca_ix_hashes = set(h.strip() for h in os.getenv("ORCA_CREATE_IX_HASHES", "").split(",") if h.strip())
        geyser_url = os.getenv("GEYSER_WS_URL", "")
        geyser_token = os.getenv("GEYSER_TOKEN", "")

        def on_message(ws, message):
            try:
                data = _json.loads(message)
                if "params" in data:
                    # On any log hit, do a quick latest-pairs scan
                    # First, try to extract pool addresses from logs and validate directly
                    value = data.get("params", {}).get("result", {}).get("value", {})
                    logs_list = value.get("logs", []) if isinstance(value, dict) else []
                    candidates = set()
                    ix_hash = value.get("signature") or ""
                    # Filter to known create ix hashes if provided
                    if (ix_hash and raydium_ix_hashes and ix_hash not in raydium_ix_hashes) and (ix_hash and orca_ix_hashes and ix_hash not in orca_ix_hashes):
                        pass
                    else:
                        for line in logs_list:
                            for token in self._extract_base58_candidates(line):
                                candidates.add(token)
                    for cand in list(candidates)[:5]:  # limit processing per message
                        try:
                            pair = dexscreener.get_pair_data("solana", cand)
                            # Additional heuristic: detect Raydium initialize pool log lines and pull address via regex
                            if not pair:
                                m = re.search(r"pool:\s*([1-9A-HJ-NP-Za-km-z]{32,44})", line)
                                if m:
                                    cand2 = m.group(1)
                                    pair = dexscreener.get_pair_data("solana", cand2)
                            if pair:
                                now_ms = time.time() * 1000
                                created_at = pair.get("pairCreatedAt") or 0
                                age_min = (now_ms - created_at) / 1000 / 60 if created_at else 999
                                liq = pair.get("liquidity", {}).get("usd", 0) or 0
                                if created_at and age_min <= self.new_pool_max_age_min and liq >= self.new_pool_min_liq:
                                    addr = pair.get("pairAddress") or cand
                                    if addr in self._seen_new_pools:
                                        continue
                                    token_data = {
                                        "address": addr,
                                        "liquidity_usd": liq,
                                        "price_usd": float(pair.get("priceUsd", 0) or 0),
                                        "pair_created_at": created_at,
                                        "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
                                    }
                                    cluster = {
                                        "cluster_type": "new_pool",
                                        "token_address": addr,
                                        "wallet_addresses": [],
                                        "wallet_count": 0,
                                        "smart_money_count": 0,
                                        "total_volume_usd": pair.get("volume", {}).get("h24", 0),
                                        "cluster_score": 10000,
                                        "detected_at": datetime.now(),
                                        "signal": "STRONG_BUY",
                                    }
                                    if self.telegram_chat_id and telegram_bot.enabled:
                                        telegram_bot.send_cluster_alert(self.telegram_chat_id, cluster, token_data)
                                    if trade_executor.should_trade(cluster, token_data):
                                        trade_executor.execute_buy(cluster, token_data)
                                    self._seen_new_pools.add(addr)
                        except Exception as e:
                            print(f"[Monitor] Log candidate processing error: {e}")

                    pairs = dexscreener.get_latest_pairs(chain="solana", limit=10)
                    now_ms = time.time() * 1000
                    for pair in pairs:
                        addr = pair.get("baseToken", {}).get("address") or pair.get("pairAddress")
                        if not addr or addr in self._seen_new_pools:
                            continue
                        created_at = pair.get("pairCreatedAt") or 0
                        age_min = (now_ms - created_at) / 1000 / 60 if created_at else 999
                        liq = pair.get("liquidity", {}).get("usd", 0) or 0
                        if created_at and age_min <= self.new_pool_max_age_min and liq >= self.new_pool_min_liq:
                            token_data = {
                                "address": addr,
                                "liquidity_usd": liq,
                                "price_usd": float(pair.get("priceUsd", 0) or 0),
                                "pair_created_at": created_at,
                                "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
                            }
                            self._maybe_panic_exit(token_data)
                            cluster = {
                                "cluster_type": "new_pool",
                                "token_address": addr,
                                "wallet_addresses": [],
                                "wallet_count": 0,
                                "smart_money_count": 0,
                                "total_volume_usd": pair.get("volume", {}).get("h24", 0),
                                "cluster_score": 10000,
                                "detected_at": datetime.now(),
                                "signal": "STRONG_BUY",
                            }
                            if self.telegram_chat_id and telegram_bot.enabled:
                                telegram_bot.send_cluster_alert(self.telegram_chat_id, cluster, token_data)
                            if trade_executor.should_trade(cluster, token_data):
                                trade_executor.execute_buy(cluster, token_data)
                            self._seen_new_pools.add(addr)
            except Exception as e:
                print(f"[Monitor] Logs handler error: {e}")

        def on_error(ws, error):
            print(f"[Monitor] Logs WS error: {error}")

        def on_close(ws, close_status_code, close_msg):
            print("[Monitor] Logs WS closed.")

        def on_open(ws):
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": filter_mentions},
                        {"commitment": "processed"}
                    ]
                }
                ws.send(_json.dumps(payload))
                print("[Monitor] Logs WS subscribed for Raydium/Orca.")
            except Exception as e:
                print(f"[Monitor] Logs WS open error: {e}")

        while self.running:
            try:
                ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_error=on_error, on_close=on_close, on_open=on_open)
                ws.run_forever()
            except Exception as e:
                print(f"[Monitor] Logs WS loop error: {e}")
                time.sleep(3)

    def _start_geyser_watch(self):
        """
        Run the async geyser watcher loop in a background thread.
        """
        try:
            import asyncio

            asyncio.run(self.geyser_watcher.start())
        except Exception as e:
            print(f"[Monitor] Geyser watcher error: {e}")

    @staticmethod
    def _extract_base58_candidates(line: str):
        import re
        # Base58 regex for 32-44 chars, filter by decodability
        pattern = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
        hits = re.findall(pattern, line or "")
        good = []
        for h in hits:
            try:
                base58.b58decode(h)
                good.append(h)
            except Exception:
                continue
        return good

    def _watch_geyser(self):
        """
        Basic Geyser websocket to detect program logs for pool creation (Raydium/Orca/Pump.fun) faster than RPC logs.
        """
        import websocket
        import json as _json
        if not os.getenv("GEYSER_WS_URL"):
            print("[Monitor] Geyser watch skipped: no GEYSER_WS_URL")
            return
        url = os.getenv("GEYSER_WS_URL")
        token = os.getenv("GEYSER_TOKEN", "")
        raydium_prog = os.getenv("RAYDIUM_PROGRAM_ID", "RVKd61ztZW9dqrjK5vCZH1vZ1tc665Ar72Xd1LgjAoG")
        orca_prog = os.getenv("ORCA_PROGRAM_ID", "9WwN7dBDEuDfSUdifYEYdzSsfXCMVvjJhtCmvYzuq76A")
        pumpfun_prog = os.getenv("PUMPFUN_PROGRAM_ID", "pump111111111111111111111111111111111111111")
        mentions = [raydium_prog, orca_prog, pumpfun_prog]

        def on_message(ws, message):
            try:
                data = _json.loads(message)
                if "value" in data and "logs" in data.get("value", {}):
                    logs_list = data["value"]["logs"]
                    # Reuse the same handler as logs watcher
                    now_ms = time.time() * 1000
                    for cand in self._extract_base58_candidates(" ".join(logs_list)):
                        try:
                            pair = dexscreener.get_pair_data("solana", cand)
                            if pair:
                                addr = pair.get("pairAddress") or cand
                                if addr in self._seen_new_pools:
                                    continue
                                created_at = pair.get("pairCreatedAt") or 0
                                age_min = (now_ms - created_at) / 1000 / 60 if created_at else 999
                                liq = pair.get("liquidity", {}).get("usd", 0) or 0
                                if created_at and age_min <= self.new_pool_max_age_min and liq >= self.new_pool_min_liq:
                                    token_data = {
                                        "address": addr,
                                        "liquidity_usd": liq,
                                        "price_usd": float(pair.get("priceUsd", 0) or 0),
                                        "pair_created_at": created_at,
                                        "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
                                        "fdv": pair.get("fdv"),
                                    }
                                    self._maybe_panic_exit(token_data)
                                    cluster = {
                                        "cluster_type": "new_pool",
                                        "token_address": addr,
                                        "wallet_addresses": [],
                                        "wallet_count": 0,
                                        "smart_money_count": 0,
                                        "total_volume_usd": pair.get("volume", {}).get("h24", 0),
                                        "cluster_score": 10000,
                                        "detected_at": datetime.now(),
                                        "signal": "STRONG_BUY",
                                    }
                                    if self.telegram_chat_id and telegram_bot.enabled:
                                        telegram_bot.send_cluster_alert(self.telegram_chat_id, cluster, token_data)
                                    if trade_executor.should_trade(cluster, token_data):
                                        trade_executor.execute_buy(cluster, token_data)
                                    self._seen_new_pools.add(addr)
            except Exception as e:
                print(f"[Monitor] Geyser handler error: {e}")

        def on_error(ws, error):
            print(f"[Monitor] Geyser WS error: {error}")

        def on_close(ws, close_status_code, close_msg):
            print("[Monitor] Geyser WS closed.")

        def on_open(ws):
            try:
                sub = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": mentions},
                        {"commitment": "processed"}
                    ]
                }
                if token:
                    ws.send(_json.dumps({"jsonrpc": "2.0", "id": 0, "method": "auth", "params": [token]}))
                ws.send(_json.dumps(sub))
                print("[Monitor] Geyser logs subscribed.")
            except Exception as e:
                print(f"[Monitor] Geyser open error: {e}")

        while self.running:
            try:
                ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close, on_open=on_open)
                ws.run_forever()
            except Exception as e:
                print(f"[Monitor] Geyser WS loop error: {e}")
                time.sleep(3)

    def _maybe_panic_exit(self, token_data: Dict[str, Any]):
        """
        Trigger panic sell if price/liquidity drop thresholds are breached for held positions.
        """
        try:
            addr = token_data.get("address") or token_data.get("token_address")
            if not addr:
                return
            if addr not in trade_executor._positions:
                return
            price_drop = token_data.get("price_change_5m")
            liq = token_data.get("liquidity_usd", 0)
            if price_drop is not None and price_drop <= -self.rug_price_drop_pct:
                print(f"[Panic] Price drop {price_drop}% detected for {addr}, exiting.")
                trade_executor.panic_sell(addr)
                return
            if liq and liq <= self.rug_liq_threshold_usd:
                print(f"[Panic] Liquidity {liq} below threshold for {addr}, exiting.")
                trade_executor.panic_sell(addr)
        except Exception as e:
            print(f"[Monitor] Panic exit check error: {e}")

    def _cluster_passes_filters(self, cluster: Dict[str, Any], token_data: Optional[Dict[str, Any]]) -> bool:
        if token_data:
            sym = (token_data.get("symbol") or "").lower()
            addr = cluster.get("token_address", "")
            if "lp" in sym and addr not in self.lp_whitelist:
                print("[Cluster] Blocked: LP token not whitelisted")
                return False
            if self.min_sol_liq > 0 and token_data.get("liquidity_sol") is not None:
                try:
                    liq_sol = float(token_data.get("liquidity_sol") or 0)
                    if liq_sol < self.min_sol_liq:
                        print(f"[Cluster] Blocked: SOL liq {liq_sol} < min {self.min_sol_liq}")
                        return False
                except Exception:
                    pass
            if self.min_sol_liq > 0 and cluster.get("pool_depth_sol") is not None:
                try:
                    if float(cluster["pool_depth_sol"]) < self.min_sol_liq:
                        print(f"[Cluster] Blocked: Pool depth SOL {cluster['pool_depth_sol']} < min {self.min_sol_liq}")
                        return False
                except Exception:
                    pass
            dec = token_data.get("decimals")
            if dec is not None:
                try:
                    d = int(dec)
                    if d < self.min_base_decimals or d > self.max_base_decimals:
                        print(f"[Cluster] Blocked: decimals {d} outside [{self.min_base_decimals},{self.max_base_decimals}]")
                        return False
                except Exception:
                    pass
        return True

    def _apply_cluster_boosts(self, cluster: Dict[str, Any], token_data: Optional[Dict[str, Any]]):
        score = int(cluster.get("cluster_score", 0))
        wallets = max(1, cluster.get("wallet_count") or 1)
        sm = cluster.get("smart_money_count") or 0
        overlap = sm / wallets if wallets else 0
        if overlap >= self.sm_overlap_threshold:
            score += self.sm_overlap_boost
        if self.safety_penalty_enabled and getattr(trade_executor, "safety_checker", None):
            try:
                res = trade_executor._run_coro(trade_executor.safety_checker.check_token(cluster["token_address"]))
                if res and res.warnings:
                    score = max(0, score - self.safety_penalty_points)
            except Exception:
                pass
        # Cadence boost: repeat smart wallets within window
        cadence_boost = self._cadence_boost(cluster)
        score += cadence_boost
        # ML blend (optional)
        if self.ml_blend_weight > 0 and self.ml_coeffs:
            try:
                model_score = self._ml_score(cluster)
                score = int((1 - self.ml_blend_weight) * score + self.ml_blend_weight * model_score)
            except Exception:
                pass
        cluster["cluster_score"] = min(10000, score)

    def _raydium_depth(self, token_mint: str):
        """
        Best-effort fetch of Raydium pool depth for SOL/token pair.
        Returns (depth_sol, depth_usd)
        """
        try:
            from solders.pubkey import Pubkey

            sol = Pubkey.from_string("So11111111111111111111111111111111111111112")
            mint = Pubkey.from_string(token_mint)
            pool, _ = trade_executor.raydium._get_pool_for_pair(sol, mint)
            if not pool:
                return None, None
            trade_executor.raydium._fetch_vault_balances(pool)
            depth_sol = pool.base_reserve / 1e9 if getattr(pool, "base_mint", None) == sol else pool.quote_reserve / 1e9
            # Rough USD depth using SOL price if available
            try:
                sol_price = trade_executor._approx_sol_usd()
                depth_usd = depth_sol * sol_price
            except Exception:
                depth_usd = None
            return depth_sol, depth_usd
        except Exception:
            return None, None

    def _update_liquidity_deltas(self, token: str, liq_usd: float, liq_sol: Optional[float]):
        """
        Track liquidity history and compute 5m/30m deltas (USD and SOL).
        """
        now = time.time()
        hist = self._liq_history.get(token, [])
        hist.append((now, liq_usd, liq_sol))
        # Keep last 60 minutes of samples
        cutoff = now - 3600
        hist = [h for h in hist if h[0] >= cutoff]
        self._liq_history[token] = hist

        def delta_for(window_sec):
            past = [h for h in hist if h[0] <= now - window_sec]
            if not past:
                return None, None
            ts, usd, sol = past[-1]
            return liq_usd - (usd or 0), (liq_sol - sol) if (liq_sol is not None and sol is not None) else None

        d5_usd, d5_sol = delta_for(300)
        d30_usd, d30_sol = delta_for(1800)
        out = {}
        if d5_usd is not None:
            out["liq_delta_5m_usd"] = d5_usd
        if d30_usd is not None:
            out["liq_delta_30m_usd"] = d30_usd
        if d5_sol is not None:
            out["liq_delta_5m_sol"] = d5_sol
        if d30_sol is not None:
            out["liq_delta_30m_sol"] = d30_sol
        return out

    def _update_holder_growth(self, token: str, holder_count: Optional[int], unique_24h: Optional[int]):
        """
        Track holder/unique wallet counts and compute 24h growth delta if possible.
        """
        now = time.time()
        prev = self._holder_history.get(token)
        out = {}
        if holder_count is not None:
            out["holder_count"] = holder_count
        if unique_24h is not None:
            out["unique_wallets_24h"] = unique_24h
        if prev:
            pts, ph, pu = prev
            if holder_count is not None and ph is not None:
                out["holder_growth_24h"] = holder_count - ph
            if unique_24h is not None and pu is not None:
                out["unique_wallets_24h_delta"] = unique_24h - pu
        self._holder_history[token] = (now, holder_count, unique_24h)
        return out

    def _cadence_boost(self, cluster: Dict[str, Any]) -> int:
        token = cluster.get("token_address")
        if not token:
            return 0
        sm_wallets = cluster.get("wallet_addresses") or []
        now = time.time()
        seen = self._smart_seen.get(token, {})
        repeat = 0
        window = self.cadence_window_min * 60
        for w in sm_wallets:
            last = seen.get(w)
            if last and now - last <= window:
                repeat += 1
            seen[w] = now
        self._smart_seen[token] = seen
        if repeat >= self.cadence_min_repeats:
            return self.cadence_boost_points
        return 0

    def _ml_score(self, cluster: Dict[str, Any]) -> float:
        """
        Compute logistic model score scaled to 0-10000.
        Feature order: liq_usd, liq_delta_5m_usd, liq_delta_30m_usd, holder_growth_24h,
        unique_wallets_24h, pool_age_minutes, smart_overlap
        """
        feats = [
            float(cluster.get("liquidity_usd") or 0),
            float(cluster.get("liq_delta_5m_usd") or 0),
            float(cluster.get("liq_delta_30m_usd") or 0),
            float(cluster.get("holder_growth_24h") or 0),
            float(cluster.get("unique_wallets_24h") or 0),
            float(cluster.get("pool_age_minutes") or 0),
            float((cluster.get("smart_money_count") or 0) / max(1, cluster.get("wallet_count") or 1)),
        ]
        n = min(len(self.ml_coeffs), len(feats))
        z = self.ml_intercept + sum(self.ml_coeffs[i] * feats[i] for i in range(n))
        import math

        prob = 1 / (1 + math.exp(-z))
        return prob * 10000

    def _cluster_to_pool_event(self, cluster: Dict[str, Any], token_data: Optional[Dict[str, Any]]):
        """
        Build a minimal NewPoolEvent-like dict for snipe executor from cluster info.
        """
        from geyser_watcher import NewPoolEvent
        from datetime import datetime

        token = cluster.get("token_address")
        liq_sol = cluster.get("liquidity_sol") or 0
        return NewPoolEvent(
            pool_type="raydium_create",
            pool_address="unknown",
            token_mint=token,
            base_mint=token,
            quote_mint="So11111111111111111111111111111111111111112",
            initial_liquidity_sol=float(liq_sol) if liq_sol else 0.0,
            signature="cluster_synthetic",
            slot=0,
            timestamp=datetime.now(),
            raw_data={},
        )

    def _watch_telegram_commands(self):
        """Poll Telegram for simple commands: /pause, /resume, /flatten"""
        offset = None
        chat = self.telegram_chat_id
        while self.running and telegram_bot.enabled and chat:
            try:
                updates = telegram_bot.fetch_updates(offset=offset)
                for upd in updates:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip().lower()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != chat:
                        continue
                    if text == "/pause":
                        open("pause.flag", "w").close()
                        telegram_bot.send_plain(chat, "⏸️ Trading paused (pause.flag created).")
                    elif text == "/resume":
                        if os.path.exists("pause.flag"):
                            os.remove("pause.flag")
                        telegram_bot.send_plain(chat, "▶️ Trading resumed (pause.flag removed).")
                    elif text == "/flatten":
                        open("flatten.flag", "w").close()
                        telegram_bot.send_plain(chat, "🔻 Flatten requested (flatten.flag created).")
                time.sleep(2)
            except Exception as e:
                print(f"[Monitor] Telegram command watch error: {e}")
                time.sleep(3)

    def _start_kol_watch(self):
        try:
            asyncio.run(self.kol_watcher.start())
        except Exception as e:
            print(f"[Monitor] KOL watcher error: {e}")

    def _start_bundle_watch(self):
        try:
            asyncio.run(self.bundle_detector.start())
        except Exception as e:
            print(f"[Monitor] Bundle detector error: {e}")

    def _load_kol_wallets(self) -> Dict[str, str]:
        wallets = {}
        kol_str = os.getenv("KOL_WALLETS", "")
        if kol_str:
            for entry in kol_str.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                if ":" in entry:
                    addr, name = entry.split(":", 1)
                    wallets[addr.strip()] = name.strip()
                else:
                    wallets[entry] = entry
        kol_file = os.getenv("KOL_WALLETS_FILE", "")
        if kol_file and os.path.exists(kol_file):
            try:
                with open(kol_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        if ":" in line:
                            addr, name = line.split(":", 1)
                            wallets[addr.strip()] = name.strip()
                        else:
                            wallets[line] = line
            except Exception:
                pass
        return wallets
    
    def _check_graduations(self):
        """Check for token graduations (Pump.fun -> Raydium)"""
        try:
            # Get active clusters from database
            active_clusters = db.get_active_clusters()
            
            for cluster in active_clusters:
                token_address = cluster['token_address']
                
                # Check graduation status
                graduation = dexscreener.check_graduation_status(token_address)
                
                if graduation.get('graduated') and graduation.get('signal') == 'STRONG_BUY':
                    # Token graduated with strong buy signal!
                    print(f"  🚀 GRADUATION DETECTED: {token_address}")
                    
                    # Update cluster status
                    db.update_cluster_status(cluster['id'], 'triggered')
                    
                    # Send Telegram alert
                    if self.telegram_chat_id and telegram_bot.enabled:
                        token_data = dexscreener.get_token_data('solana', token_address)
                        telegram_bot.send_graduation_alert(
                            self.telegram_chat_id,
                            token_address,
                            graduation,
                            token_data
                        )
                        print(f"     📱 Graduation alert sent!")
        
        except Exception as e:
            print(f"  ❌ Graduation check error: {e}")
    
    def _monitor_tracked_wallets(self):
        """Monitor activity of tracked wallets"""
        try:
            # Get all user preferences
            # For now, we'll skip this since we don't have user management
            # This would check tracked wallets and alert on their activity
            pass
        
        except Exception as e:
            print(f"  ❌ Wallet monitoring error: {e}")
    
    def stop(self):
        """Stop the monitoring service"""
        self.running = False
        print("\n[Monitor] Service stopped")

if __name__ == '__main__':
    # Initialize database
    db.init_database()
    
    # Create and start monitoring service
    monitor = MonitoringService()
    
    try:
        monitor.start()
    except KeyboardInterrupt:
        monitor.stop()

