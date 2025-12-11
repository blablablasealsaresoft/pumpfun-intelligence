"""
Trade Execution Coordinator

This module keeps auto-trading logic separated and defaults to a safe dry-run.
It uses Jupiter quotes for price/route discovery and enforces basic safeguards
before attempting any broadcast. Real transaction submission is intentionally
left as a stub so the wallet secret never leaves the operator's environment
unless explicitly wired up.
"""

from __future__ import annotations

import base64
import os
import time
import threading
import json
import pathlib
from typing import Dict, Any, Optional, List

import requests
from base58 import b58decode
from solana.keypair import Keypair
from solana.rpc.api import Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.transaction import VersionedTransaction, TransactionInstruction
from solders.pubkey import Pubkey
from solders.keypair import Keypair as SoldersKeypair
import asyncio

from dexscreener_api import dexscreener
from risk_sources import evaluate_token
from telegram_service import telegram_bot
from raydium_direct import RaydiumDirect
from raydium_direct.amm_math import calculate_swap_output
from trading import (
    calculate_optimal_buy_size,
    SizingParams,
    simulate_sell,
    PriorityFeeTuner,
    CongestionMonitor,
    AutoPauseManager,
    TokenSafetyChecker,
    SafetyConfig,
    TradeMetrics,
    metrics_collector,
)
from risk_sources import tokensniffer_report, rugdoc_report
from position_manager import PositionManager, ExitReason, Position

# SOL mint for Jupiter quoting
SOL_MINT = "So11111111111111111111111111111111111111112"


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class TradeExecutor:
    def __init__(self):
        # Feature flags
        self.enabled = _env_bool("ALLOW_AUTOTRADE", True) and _env_bool("AUTO_TRADE_ENABLED", True)
        # Dry-run defaults to False; set DRY_RUN=true to simulate
        self.dry_run = _env_bool("DRY_RUN", False) or not _env_bool("ALLOW_BROADCAST", True)
        self.speed_mode = _env_bool("SPEED_MODE", False)
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "8"))

        # RPC + signing
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.rpc_clients: List[Client] = self._build_rpc_clients()
        self.kill_switch = _env_bool("KILL_SWITCH", False)
        if self.kill_switch:
            raise RuntimeError("KILL_SWITCH is enabled. Disable to run auto-trade.")
        self.keypair: Optional[Keypair] = self._load_keypair_from_env()
        if not self.keypair:
            raise RuntimeError("WALLET_PRIVATE_KEY missing or invalid. Auto-trade requires a loaded keypair.")
        self.public_key_str = str(self.keypair.public_key)
        try:
            self.solders_keypair = SoldersKeypair.from_bytes(bytes(self.keypair.secret_key))
        except Exception:
            self.solders_keypair = None
        self.raydium_direct = RaydiumDirect(self.rpc_clients[0], self.keypair)

        # Basic sizing and risk
        self.default_buy_sol = float(os.getenv("DEFAULT_BUY_AMOUNT_SOL", "0.1"))
        self.min_liquidity_usd = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
        self.min_cluster_score = int(float(os.getenv("AUTO_SNIPE_MIN_SCORE", "80")) * 100)  # score is 0-10000
        self.max_daily_trades = int(os.getenv("AUTO_TRADE_MAX_DAILY_TRADES", "0"))  # 0 = unlimited
        self.min_pool_age_minutes = int(os.getenv("MIN_POOL_AGE_MINUTES", "2"))
        self.max_price_impact_pct = float(os.getenv("MAX_PRICE_IMPACT_PCT", "20"))
        self.max_5m_drop_pct = float(os.getenv("MAX_5M_DROP_PCT", "15"))
        self.helius_max_tx_age_min = float(os.getenv("HELIUS_MAX_TX_AGE_MINUTES", "15"))
        self.max_per_token_sol = float(os.getenv("MAX_PER_TOKEN_SOL", "2.0"))
        self.max_global_sol = float(os.getenv("MAX_GLOBAL_SOL", "5.0"))
        self.balance_sizing_pct = float(os.getenv("BALANCE_SIZING_PCT", "0"))  # 0 = disabled; percent of SOL balance
        self.max_per_trade_sol = float(os.getenv("MAX_PER_TRADE_SOL", "3.0"))
        self.max_liq_pct_per_trade = float(os.getenv("MAX_LIQ_PCT_PER_TRADE", "2.5"))  # % of liquidity
        self.min_holders = int(os.getenv("MIN_HOLDERS", "0"))
        self.min_fdv_usd = float(os.getenv("MIN_FDV_USD", "0"))
        self.max_fdv_usd = float(os.getenv("MAX_FDV_USD", "0"))  # 0 = no cap
        self.max_trade_usd = float(os.getenv("MAX_TRADE_USD", "0"))  # 0 = disabled
        self.low_fdv_slippage_bps = int(os.getenv("LOW_FDV_SLIPPAGE_BPS", "400"))  # 4% for low FDV tokens
        self.low_fdv_threshold_usd = float(os.getenv("LOW_FDV_THRESHOLD_USD", "500000"))  # FDV under this uses tighter slippage
        self.high_fdv_slippage_bps = int(os.getenv("HIGH_FDV_SLIPPAGE_BPS", self.slippage_bps_base))
        self.high_fdv_threshold_usd = float(os.getenv("HIGH_FDV_THRESHOLD_USD", "5000000"))
        self.max_buy_tax_pct = float(os.getenv("MAX_BUY_TAX_PCT", "15"))
        self.max_sell_tax_pct = float(os.getenv("MAX_SELL_TAX_PCT", "15"))
        self.require_renounce_owner = _env_bool("REQUIRE_RENOUNCE_OWNER", False)
        self.allow_proxy = _env_bool("ALLOW_PROXY_CONTRACT", True)
        self.direct_route_only = _env_bool("DIRECT_ROUTE_ONLY", True)
        self.dex_preference = [d.strip() for d in os.getenv("DEX_PREFERENCE", "raydium,orca").split(",") if d.strip()]
        self.require_direct_dex = _env_bool("REQUIRE_DIRECT_DEX", False)
        self.enable_raydium_direct = _env_bool("ENABLE_RAYDIUM_DIRECT", False)
        # DCA
        self.dca_enabled = _env_bool("DCA_ENABLED", False)
        self.dca_tranches = int(os.getenv("DCA_TRANCHES", "3"))
        self.dca_interval_sec = int(os.getenv("DCA_INTERVAL_SEC", "10"))
        # Sentiment gate
        self.sentiment_enabled = _env_bool("SENTIMENT_ENABLED", False)
        self.sentiment_api = os.getenv("SENTIMENT_API_URL", "https://cryptopanic.com/api/v1/posts/")
        self.sentiment_token = os.getenv("SENTIMENT_API_TOKEN", "")
        self.sentiment_keywords = [k.strip() for k in os.getenv("SENTIMENT_KEYWORDS", "solana,pump fun,pumpfun").split(",") if k.strip()]
        self.min_sentiment_score = float(os.getenv("MIN_SENTIMENT_SCORE", "0.0"))
        self.skip_preflight = _env_bool("SKIP_PREFLIGHT", False)
        self.pause_file = os.getenv("PAUSE_FILE", "pause.flag")
        self.flatten_file = os.getenv("FLATTEN_FILE", "flatten.flag")
        self.positions_log = os.getenv("POSITIONS_LOG", os.path.join("logs", "positions.jsonl"))
        self.pnl_log = os.getenv("PNL_LOG", os.path.join("logs", "pnl.jsonl"))
        self.compute_unit_limit = int(os.getenv("COMPUTE_UNIT_LIMIT", "0"))
        self.panic_tip_lamports = int(os.getenv("PANIC_TIP_LAMPORTS", "300000"))  # 0.0003 SOL
        self.panic_slippage_bps = int(os.getenv("PANIC_SLIPPAGE_BPS", "1500"))  # 15%
        self.panic_priority_fee = int(os.getenv("PANIC_PRIORITY_FEE_MICROLAMPORTS", "800000"))  # 0.0008 SOL
        self.max_slippage_bps_cap = int(os.getenv("MAX_SLIPPAGE_BPS_CAP", "2000"))  # 20%
        self.max_panic_slippage_bps_cap = int(os.getenv("MAX_PANIC_SLIPPAGE_BPS_CAP", "3000"))  # 30%
        self.require_renounce_mint = _env_bool("REQUIRE_RENOUNCE_MINT", False)
        self.require_renounce_freeze = _env_bool("REQUIRE_RENOUNCE_FREEZE", False)
        self.rug_price_drop_pct = float(os.getenv("RUG_PRICE_DROP_PCT", "35"))
        self.rug_liq_threshold_usd = float(os.getenv("RUG_LIQ_THRESHOLD_USD", "2000"))

        # Adaptive slippage / fees / retries
        self.max_swap_retries = int(os.getenv("MAX_SWAP_RETRIES", "2"))
        self.slippage_bps_base = int(os.getenv("SLIPPAGE_BPS_BASE", "500"))  # 5%
        self.slippage_bps_step = int(os.getenv("SLIPPAGE_BPS_STEP", "200"))  # +2% per retry
        self.priority_fee_microlamports = int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "0") or 0)
        self.priority_fee_step = int(os.getenv("PRIORITY_FEE_STEP_MICROLAMPORTS", "50000"))
        self.enable_dynamic_sizing = os.getenv("ENABLE_DYNAMIC_SIZING", "false").lower() in {"1", "true", "yes", "on"}
        self.enable_sell_simulation = os.getenv("ENABLE_SELL_SIMULATION", "true").lower() in {"1", "true", "yes", "on"}
        self.max_round_trip_bps = int(os.getenv("MAX_ROUND_TRIP_BPS", "1000") or 1000)
        self.round_trip_hard_limit_bps = int(os.getenv("ROUND_TRIP_HARD_LIMIT_BPS", "2000") or 2000)
        self.enable_fee_tuner = os.getenv("ENABLE_FEE_TUNER", "true").lower() in {"1", "true", "yes", "on"}
        self.enable_auto_pause = os.getenv("ENABLE_AUTO_PAUSE", "true").lower() in {"1", "true", "yes", "on"}
        self.fee_tuner: Optional[PriorityFeeTuner] = PriorityFeeTuner() if self.enable_fee_tuner else None
        self.congestion_monitor: Optional[CongestionMonitor] = (
            CongestionMonitor(self.rpc_clients[0]) if self.enable_fee_tuner else None
        )
        self.pause_manager: Optional[AutoPauseManager] = (
            AutoPauseManager(
                rpc_client=self.rpc_clients[0],
                wallet_pubkey=Pubkey.from_string(self.public_key_str),
                on_pause=self._on_trading_paused,
                on_resume=self._on_trading_resumed,
            )
            if self.enable_auto_pause
            else None
        )

        # Ownership/upgradeability safety checks (Item #4)
        self.enable_token_safety = _env_bool("ENABLE_TOKEN_SAFETY_CHECKS", True)
        self.safety_checker: Optional[TokenSafetyChecker] = None
        if self.enable_token_safety:
            self.safety_checker = TokenSafetyChecker(
                rpc_client=AsyncClient(self.rpc_url),
                config=SafetyConfig(
                    require_mint_renounced=_env_bool("REQUIRE_MINT_RENOUNCED", True),
                    require_freeze_renounced=_env_bool("REQUIRE_FREEZE_RENOUNCED", True),
                    require_metadata_immutable=_env_bool("REQUIRE_METADATA_IMMUTABLE", False),
                    allow_token_2022=_env_bool("ALLOW_TOKEN_2022", False),
                    whitelist_mints=self._load_whitelist(),
                ),
            )

        # Observability / metrics (Item #5)
        self.enable_metrics = _env_bool("ENABLE_METRICS", True)
        self.slow_trade_threshold_ms = float(os.getenv("SLOW_TRADE_THRESHOLD_MS", "2000"))
        self.alert_chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))
        self.alert_on_success = _env_bool("TELEGRAM_ALERT_ON_SUCCESS", False)
        self.alert_on_failure = _env_bool("TELEGRAM_ALERT_ON_FAILURE", True)
        self.alert_on_pause = _env_bool("TELEGRAM_ALERT_ON_PAUSE", True)
        self.alert_on_slow_trade = _env_bool("TELEGRAM_ALERT_ON_SLOW_TRADE", True)
        self.alert_on_safety_block = _env_bool("TELEGRAM_ALERT_ON_SAFETY_BLOCK", True)

        # Jito / bundle
        self.enable_jito = _env_bool("ENABLE_JITO_BUNDLES", False)
        self.jito_url = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")
        self.jito_tip_lamports = int(os.getenv("JITO_TIP_LAMPORTS", "100000"))  # 0.0001 SOL
        self.jito_tip_min = int(os.getenv("JITO_MIN_TIP_LAMPORTS", str(self.jito_tip_lamports)))
        self.jito_tip_max = int(os.getenv("JITO_MAX_TIP_LAMPORTS", str(max(self.jito_tip_lamports, self.jito_tip_min))))
        self.jito_dynamic_tip = _env_bool("JITO_DYNAMIC_TIP", False)

        # Exit management
        self.take_profit_pct = float(os.getenv("TAKE_PROFIT_PERCENT", "75"))
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PERCENT", "15"))
        self.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PERCENT", "10"))
        self.trailing_activation_pct = float(os.getenv("TRAILING_STOP_ACTIVATION", "20"))
        self.exit_timeout_minutes = float(os.getenv("EXIT_TIMEOUT_MINUTES", "60"))
        self.price_poll_seconds = float(os.getenv("PRICE_POLL_SECONDS", "15"))

        # Price feeds
        self.enable_pyth_price = _env_bool("PYTH_PRICE_FEED_ENABLED", False)
        self.pyth_endpoint = os.getenv("PYTH_API_URL", "https://hermes.pyth.network/api")
        self.pyth_sol_feed_id = os.getenv(
            "PYTH_SOL_USD_FEED_ID",
            "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
        )

        # Housekeeping
        self._trades_today = 0
        self._day_started = time.strftime("%Y-%m-%d")
        self._active_positions = {}
        self._positions: Dict[str, Dict[str, Any]] = {}
        pathlib.Path("logs").mkdir(exist_ok=True)
        # Async loop for internal coroutines (avoid asyncio.run)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

        # Position manager (auto-exits & PnL)
        self.enable_position_manager = _env_bool("ENABLE_POSITION_MANAGER", True)
        self.position_manager: Optional[PositionManager] = None
        if self.enable_position_manager and self.solders_keypair:
            try:
                self.position_manager = PositionManager(
                    rpc_client=AsyncClient(self.rpc_url),
                    keypair=self.solders_keypair,
                    on_exit=self._on_position_exit,
                )
                self._run_coro(self.position_manager.start())
                print("[Executor] PositionManager started")
            except Exception as e:
                print(f"[Executor] PositionManager init error: {e}")

        # Apply speed mode overrides
        if self.speed_mode:
            self.slippage_bps_base = int(os.getenv("SPEED_SLIPPAGE_BPS_BASE", self.slippage_bps_base))
            self.slippage_bps_step = int(os.getenv("SPEED_SLIPPAGE_BPS_STEP", self.slippage_bps_step))
            self.priority_fee_microlamports = int(os.getenv("SPEED_PRIORITY_FEE_MICROLAMPORTS", self.priority_fee_microlamports or 500000))
            self.priority_fee_step = int(os.getenv("SPEED_PRIORITY_FEE_STEP_MICROLAMPORTS", self.priority_fee_step or 100000))
            self.max_swap_retries = int(os.getenv("SPEED_MAX_SWAP_RETRIES", self.max_swap_retries or 2))
            self.enable_jito = _env_bool("SPEED_ENABLE_JITO_BUNDLES", True) or self.enable_jito
            self.jito_tip_lamports = int(os.getenv("SPEED_JITO_TIP_LAMPORTS", self.jito_tip_lamports or 300000))
            self.jito_tip_min = max(self.jito_tip_min, self.jito_tip_lamports)
            self.jito_tip_max = max(self.jito_tip_max, self.jito_tip_min)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def should_trade(self, cluster: Dict[str, Any], token_data: Optional[Dict[str, Any]]) -> bool:
        """
        Decide if an auto trade should be attempted for this cluster.
        """
        self._reset_daily_counters_if_needed()

        if not self.enabled:
            return False

        if self.pause_file and os.path.exists(self.pause_file):
            print("[Executor] Pause file present; skipping trades.")
            return False

        if cluster.get("cluster_score", 0) < self.min_cluster_score:
            return False

        # Liquidity floor
        liquidity_ok = True
        liquidity_val = 0
        if token_data:
            liquidity_val = float(token_data.get("liquidity_usd", 0) or 0)
            liquidity_ok = liquidity_val >= self.min_liquidity_usd

        if not liquidity_ok:
            print(f"[Executor] Skip auto-trade: liquidity ${liquidity_val:,.0f} < ${self.min_liquidity_usd:,.0f}")
            return False

        # Pool age and short-term price sanity (from DexScreener)
        if token_data:
            pair_created_at = token_data.get("pair_created_at") or 0
            if pair_created_at:
                age_minutes = max(0, (time.time() - (pair_created_at / 1000)) / 60)
                if age_minutes < self.min_pool_age_minutes:
                    print(f"[Executor] Skip auto-trade: pool age {age_minutes:.1f}m < {self.min_pool_age_minutes}m")
                    return False
            pct_5m = token_data.get("price_change_5m")
            if pct_5m is not None and pct_5m < -self.max_5m_drop_pct:
                print(f"[Executor] Skip auto-trade: price -5m drop {pct_5m}% exceeds {self.max_5m_drop_pct}%")
                return False

        # Helius freshness check (best effort)
        try:
            from risk_sources import helius_latest_tx_age_minutes
            age = helius_latest_tx_age_minutes(cluster["token_address"])
            if age is not None and age > self.helius_max_tx_age_min:
                print(f"[Executor] Skip auto-trade: Helius latest tx age {age:.1f}m > {self.helius_max_tx_age_min}m")
                return False
        except Exception:
            pass

        # External risk checks
        risk_view = evaluate_token(cluster["token_address"])
        if risk_view["risk_level"] in {"HIGH", "CRITICAL"}:
            print(f"[Executor] Skip auto-trade: external risk {risk_view['risk_level']} findings={risk_view['findings']}")
            return False

        if self.max_daily_trades and self._trades_today >= self.max_daily_trades:
            print(f"[Executor] Skip auto-trade: max daily trades reached ({self._trades_today}/{self.max_daily_trades})")
            return False

        return True

    def execute_buy(self, cluster: Dict[str, Any], token_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Attempt (or simulate) a buy using Jupiter quote. Real TX submission is a stub.
        """
        token_mint = cluster["token_address"]
        start_ts = time.time()
        metrics = self._init_metrics(token_mint, cluster, start_ts)

        if self.pause_manager:
            allowed, reason = self.pause_manager.is_trading_allowed()
            if not allowed:
                return {"status": "paused", "reason": reason}
            bal, is_crit = self._run_coro(self.pause_manager.check_balance()) or (None, False)
            if is_crit:
                return {"status": "paused", "reason": "critical_balance", "balance": bal}

        # Exposure checks
        if not self._exposure_allows(token_data):
            return {"status": "blocked_exposure", "reason": "exposure_limit"}

        # Pre-trade sellability/risk checks
        if not self._pretrade_checks(token_mint):
            return {"status": "blocked_pretrade", "reason": "pretrade_checks_failed"}

        # Ownership/authority safety checks (mint/freeze/metadata)
        if self.enable_token_safety and self.safety_checker:
            safety_start = time.time()
            safety_result = self._run_coro(self.safety_checker.check_token(token_mint))
            if not safety_result:
                return {"status": "blocked", "reason": "token_safety_unavailable"}
            if metrics:
                metrics.safety_check_ms = (time.time() - safety_start) * 1000
                metrics.safety_check_passed = safety_result.is_safe
                metrics.safety_warnings = safety_result.warnings
            if not safety_result.is_safe:
                print(f"[Safety] Token {token_mint[:8]}... blocked: {safety_result.warnings}")
                try:
                    metrics_collector.record_safety_block(len(safety_result.warnings))
                except Exception:
                    pass
                if self.alert_on_safety_block and self.alert_chat_id:
                    telegram_bot.send_safety_blocked(self.alert_chat_id, token_mint, safety_result.warnings, token_data)
                return {
                    "status": "blocked",
                    "reason": "token_safety",
                    "warnings": safety_result.warnings,
                }
            if safety_result.warnings:
                print(f"[Safety] Token {token_mint[:8]}... warnings: {safety_result.warnings}")

        if self.enable_dynamic_sizing:
            pool_state = self._get_raydium_pool(token_mint)
            if pool_state:
                result = self._execute_buy_with_sizing(cluster, token_data, pool_state)
                if metrics:
                    metrics.path = "raydium_direct"
                    metrics.sizing_method = "dynamic"
                    metrics.actual_amount_sol = (
                        float(result.get("amount_lamports", 0)) / 1e9 if isinstance(result, dict) else 0.0
                    )
                    metrics.signature = (
                        result.get("tx_signature")
                        or result.get("result")
                        if isinstance(result, dict)
                        else None
                    )
                    metrics.success = result.get("status") in {"sent_raydium", "dry_run_raydium", "sent"}
                    metrics.error_type = None if metrics.success else result.get("status")
                    metrics.total_latency_ms = (time.time() - start_ts) * 1000
                try:
                    metrics_collector.record_trade(
                        path=metrics.path,
                        success=metrics.success,
                        latency_ms=metrics.total_latency_ms,
                        reason=metrics.error_type,
                    )
                except Exception:
                    pass
                    self._post_trade_alerts_and_metrics(metrics, token_data)
                return result
            else:
                print("[Sizing] Pool not found; falling back to fixed-size path.")

        buy_size = self._determine_buy_size(token_data)
        if metrics:
            metrics.requested_amount_sol = buy_size
        # Slippage override for low FDV tokens
        slippage_bps = self.slippage_bps_base
        if token_data and token_data.get("fdv"):
            fdv = float(token_data["fdv"] or 0)
            if fdv > 0 and fdv < self.low_fdv_threshold_usd:
                slippage_bps = min(slippage_bps, self.low_fdv_slippage_bps)
            elif fdv >= self.high_fdv_threshold_usd and self.high_fdv_slippage_bps:
                slippage_bps = min(slippage_bps, self.high_fdv_slippage_bps)

        # Sentiment gate
        if self.sentiment_enabled and not self._sentiment_ok(cluster["token_address"]):
            return {**cluster, "status": "blocked_sentiment"}

        # DCA mode: split into tranches
        if self.dca_enabled and self.dca_tranches > 1:
            tranche_size = max(0.0001, buy_size / self.dca_tranches)
            results = []
            for i in range(self.dca_tranches):
                q = self._get_quote_full(token_mint=token_mint, slippage_bps=slippage_bps, priority_fee=self.priority_fee_microlamports, amount_sol=tranche_size)
                if not q:
                    results.append({"status": "quote_unavailable"})
                else:
                    res = self._execute_buy_with_quote(cluster, token_data, q, tranche_size)
                    results.append(res)
                if i < self.dca_tranches - 1:
                    time.sleep(self.dca_interval_sec)
            if metrics:
                metrics.path = "dca"
                metrics.attempts = len(results)
                metrics.actual_amount_sol = buy_size
                metrics.success = any(r.get("status") in {"sent", "sent_raydium", "dry_run_raydium", "sent_direct"} for r in results)
                metrics.signature = next(
                    (
                        r.get("tx_signature")
                        or r.get("result")
                        for r in results
                        if r.get("status") in {"sent", "sent_raydium", "dry_run_raydium", "sent_direct"}
                    ),
                    None,
                )
                metrics.total_latency_ms = (time.time() - start_ts) * 1000
                metrics.error_type = None if metrics.success else "dca_failed"
                metrics.error_message = None if metrics.success else "dca_failed"
                try:
                    metrics_collector.record_trade(
                        path=metrics.path,
                        success=metrics.success,
                        latency_ms=metrics.total_latency_ms,
                        reason=metrics.error_type,
                    )
                except Exception:
                    pass
                self._post_trade_alerts_and_metrics(metrics, token_data)
            return {"status": "dca_complete", "tranches": results}

        quote = self._get_quote_full(token_mint=token_mint, slippage_bps=slippage_bps, priority_fee=self.priority_fee_microlamports, amount_sol=buy_size)

        # If require direct dex and no quote (or blocked), try direct AMM swap path
        if self.require_direct_dex and not quote:
            direct_sig = self._direct_amm_swap(token_mint, buy_size)
            if direct_sig:
                result = {"status": "sent_direct", "tx_signature": direct_sig}
                if metrics:
                    metrics.path = "raydium_direct"
                    metrics.actual_amount_sol = buy_size
                    metrics.signature = direct_sig
                    metrics.success = True
                    metrics.total_latency_ms = (time.time() - start_ts) * 1000
                    try:
                        metrics_collector.record_trade(
                            path=metrics.path,
                            success=metrics.success,
                            latency_ms=metrics.total_latency_ms,
                            reason=None,
                        )
                    except Exception:
                        pass
                    self._post_trade_alerts_and_metrics(metrics, token_data)
                return result

        result = self._execute_buy_with_quote(cluster, token_data, quote, buy_size)
        if metrics:
            metrics.path = "jupiter"
            metrics.actual_amount_sol = buy_size
            metrics.signature = result.get("tx_signature")
            metrics.success = result.get("status") in {"sent"}
            metrics.error_type = None if metrics.success else result.get("status")
            metrics.error_message = result.get("reason") or result.get("status")
            metrics.total_latency_ms = (time.time() - start_ts) * 1000
            try:
                metrics_collector.record_trade(
                    path=metrics.path,
                    success=metrics.success,
                    latency_ms=metrics.total_latency_ms,
                    reason=metrics.error_type,
                )
            except Exception:
                pass
            self._post_trade_alerts_and_metrics(metrics, token_data)
        return result

    def execute_sell(self, token_address: str, amount_sol: float, panic: bool = False) -> Dict[str, Any]:
        """
        Placeholder for auto-sell logic. Mirrors execute_buy for now.
        """
        # Use stored size if available
        amount_to_sell = amount_sol
        if token_address in self._positions and self._positions[token_address].get("in_amount"):
            amount_to_sell = self._positions[token_address]["in_amount"]
        quote = self._get_quote_full(token_mint=token_address, reverse=True, amount_sol=amount_to_sell, slippage_bps=self.slippage_bps_base, priority_fee=self.priority_fee_microlamports)
        trade_plan = {
            "action": "SELL",
            "token": token_address,
            "quote": quote,
            "dry_run": self.dry_run,
        }

        if self.dry_run:
            print("[Executor] DRY-RUN sell plan prepared (no transaction broadcast).")
            return {**trade_plan, "status": "simulated"}

        if not quote:
            return {**trade_plan, "status": "quote_unavailable"}

        price_impact = None
        try:
            price_impact = float((quote.get("priceImpactPct") or 0) * 100)
        except Exception:
            pass
        if price_impact is not None and price_impact > self.max_price_impact_pct:
            return {**trade_plan, "status": "blocked_price_impact", "price_impact_pct": price_impact}

        tx_sig = self._execute_swap_with_retry(quote, reverse=True, panic=panic)
        return {**trade_plan, "status": "sent" if tx_sig else "failed", "tx_signature": tx_sig}

    def _on_trading_paused(self, reason: str, details: str):
        print(f"[AutoPause] Trading paused: {reason} ({details})")
        if self.alert_on_pause and self.alert_chat_id and telegram_bot.enabled:
            telegram_bot.send_trading_paused(self.alert_chat_id, reason, details)

    def _on_trading_resumed(self, trigger: str):
        print(f"[AutoPause] Trading resumed: {trigger}")
        if self.alert_on_pause and self.alert_chat_id and telegram_bot.enabled:
            telegram_bot.send_trading_resumed(self.alert_chat_id, trigger)

    def _get_raydium_pool(self, token_mint: str):
        """
        Best-effort fetch of Raydium pool state for SOL/token pair.
        """
        try:
            sol = Pubkey.from_string("So11111111111111111111111111111111111111112")
            mint = Pubkey.from_string(token_mint)
            pool, _ = self.raydium._get_pool_for_pair(sol, mint)
            if not pool:
                return None
            # Populate reserves
            self.raydium._fetch_vault_balances(pool)
            return pool
        except Exception as e:
            print(f"[Sizing] Failed to fetch Raydium pool: {e}")
            return None

    def _execute_buy_with_sizing(self, cluster: Dict[str, Any], token_data: Optional[Dict[str, Any]], pool_state) -> Dict[str, Any]:
        """
        Dynamic sizing + pre-sell simulation path.
        """
        try:
            sizing_params = SizingParams(
                min_buy_sol=float(os.getenv("MIN_BUY_SOL", "0.01")),
                max_buy_sol=float(os.getenv("MAX_PER_TOKEN_SOL", "2.0")),
                target_impact_bps=int(os.getenv("TARGET_IMPACT_BPS", "100")),
                max_impact_bps=int(os.getenv("MAX_PRICE_IMPACT_BPS", "500") or 500),
                max_liquidity_pct=float(os.getenv("MAX_LIQ_PCT_PER_TRADE", "2.5")),
            )

            sol_price = self._approx_sol_usd()

            sizing = calculate_optimal_buy_size(
                base_reserve=pool_state.base_reserve,
                quote_reserve=pool_state.quote_reserve,
                base_decimals=getattr(pool_state, "base_decimal", 9),
                quote_decimals=getattr(pool_state, "quote_decimal", 9),
                sol_price_usd=sol_price,
                params=sizing_params,
            )

            print(
                f"[SIZING] {sizing.recommended_amount / 1e9:.4f} SOL | "
                f"impact: {sizing.expected_impact_bps}bps | "
                f"depth: ${sizing.pool_depth_usd:,.0f} | capped: {sizing.capped_by}"
            )

            # Pre-sell simulation
            if self.enable_sell_simulation:
                expected_tokens = calculate_swap_output(
                    sizing.recommended_amount,
                    pool_state.quote_reserve,
                    pool_state.base_reserve,
                )
                sell_sim = self._run_coro(
                    simulate_sell(
                        raydium_direct=self.raydium,
                        token_mint=Pubkey.from_string(cluster["token_address"]),
                        token_amount=expected_tokens,
                        pool_state=pool_state,
                        slippage_bps=self.slippage_bps_base,
                    )
                )
                if not sell_sim:
                    print("[SELL-SIM] Simulation failed to run")
                    return {
                        "status": "blocked_exit_simulation",
                        "token": cluster["token_address"],
                        "warnings": ["sell_simulation_failed"],
                    }
                print(
                    f"[SELL-SIM] can_exit={sell_sim.can_exit} impact={sell_sim.expected_impact_bps}bps warnings={sell_sim.warnings}"
                )
                if not sell_sim.can_exit:
                    return {
                        "status": "blocked_exit_simulation",
                        "token": cluster["token_address"],
                        "warnings": sell_sim.warnings,
                    }

                round_trip_bps = sizing.expected_impact_bps + sell_sim.expected_impact_bps
                hard_limit = self.round_trip_hard_limit_bps
                if hard_limit and round_trip_bps > hard_limit:
                    return {
                        "status": "blocked_round_trip",
                        "round_trip_bps": round_trip_bps,
                        "hard_limit_bps": hard_limit,
                    }

                if round_trip_bps > self.max_round_trip_bps:
                    reduction = self.max_round_trip_bps / round_trip_bps
                    adjusted_amount = max(
                        int(sizing.recommended_amount * reduction),
                        int(sizing_params.min_buy_sol * 1e9),
                    )
                    print(
                        f"[SIZING] Adjusted for round-trip {round_trip_bps}bps -> {self.max_round_trip_bps}bps: "
                        f"{sizing.recommended_amount / 1e9:.4f} -> {adjusted_amount / 1e9:.4f} SOL"
                    )
                    sizing.recommended_amount = adjusted_amount

            # Execute via Raydium direct (with fallback handled by caller if needed)
            slippage = max(sizing.expected_impact_bps + 100, 200)  # buffer, min 2%
            try:
                sig_or_dry = self._run_coro(
                    self.raydium.try_swap(
                        token_mint=cluster["token_address"],
                        amount_sol=sizing.recommended_amount / 1e9,
                        slippage_bps=slippage,
                        priority_fee=self._current_priority_fee(),
                    )
                )
                if sig_or_dry:
                    if self.fee_tuner:
                        self.fee_tuner.record_outcome(success=True)
                    if self.pause_manager:
                        self.pause_manager.record_success()
                    return {
                        "status": "sent_raydium" if isinstance(sig_or_dry, str) else "dry_run_raydium",
                        "result": sig_or_dry if isinstance(sig_or_dry, dict) else sig_or_dry,
                        "amount_lamports": sizing.recommended_amount,
                        "expected_impact_bps": sizing.expected_impact_bps,
                    }
                else:
                    if self.fee_tuner:
                        self.fee_tuner.record_outcome(success=False, error_type="swap_failed")
                    if self.pause_manager:
                        self.pause_manager.record_failure("swap_failed")
            except Exception as e:
                if self.fee_tuner:
                    self.fee_tuner.record_outcome(success=False, error_type=self._classify_error(e))
                if self.pause_manager:
                    self.pause_manager.record_failure(self._classify_error(e))

            # Fallback to existing quote path if Raydium fails
            quote = self._get_quote_full(
                token_mint=cluster["token_address"],
                slippage_bps=slippage,
                priority_fee=self.priority_fee_microlamports,
                amount_sol=sizing.recommended_amount / 1e9,
            )
            return self._execute_buy_with_quote(cluster, token_data, quote, sizing.recommended_amount / 1e9)
        except Exception as e:
            print(f"[Sizing] Error in dynamic sizing path: {e}")
            return {"status": "sizing_error", "error": str(e)}

    def _load_whitelist(self) -> List[str]:
        """Load whitelisted mints from env string."""
        whitelist_str = os.getenv("TOKEN_WHITELIST", "")
        if whitelist_str:
            return [m.strip() for m in whitelist_str.split(",") if m.strip()]
        return []

    def _current_priority_fee(self) -> int:
        """
        Return tuned priority fee if enabled; otherwise static.
        """
        if not self.enable_fee_tuner or not self.fee_tuner:
            return self.priority_fee_microlamports
        try:
            if self.congestion_monitor:
                level = self._run_coro(self.congestion_monitor.get_congestion_level())
                if level is not None:
                    self.fee_tuner.update_congestion(level)
            return self.fee_tuner.get_current_fee()
        except Exception:
            return self.priority_fee_microlamports

    def _current_jito_tip(self, panic: bool = False, aggressive: bool = False) -> int:
        """
        Choose a Jito tip within configured band.
        """
        base = self.panic_tip_lamports if panic else self.jito_tip_lamports
        low = max(0, self.jito_tip_min)
        high = max(low, self.jito_tip_max or base)
        if not self.jito_dynamic_tip:
            return max(low, min(base, high))
        if aggressive:
            return high
        # mid-point default dynamic
        return max(low, min((low + high) // 2, high))

    def _classify_error(self, error: Exception) -> str:
        err = str(error).lower()
        if "timeout" in err:
            return "timeout"
        if "blockhash" in err:
            return "blockhash_expired"
        if "insufficient" in err:
            return "insufficient_funds"
        if "slippage" in err:
            return "slippage_exceeded"
        return "unknown"

    def panic_sell(self, token_address: str):
        """
        Panic exit for a given token using stored size if available.
        """
        if token_address not in self._positions:
            return {"status": "no_position"}
        amt = self._positions[token_address].get("in_amount", self.default_buy_sol)
        print(f"[Panic] Exiting {token_address} size {amt} SOL with panic settings")
        return self.execute_sell(token_address, amt, panic=True)

    def _execute_buy_with_quote(self, cluster: Dict[str, Any], token_data: Optional[Dict[str, Any]], quote: Optional[Dict[str, Any]], buy_size: float) -> Dict[str, Any]:
        trade_plan = {
            "action": "BUY",
            "token": cluster["token_address"],
            "cluster_score": cluster.get("cluster_score"),
            "wallets": cluster.get("wallet_count"),
            "smart_money": cluster.get("smart_money_count"),
            "quote": quote,
            "dry_run": self.dry_run,
        }

        if self.dry_run:
            print("[Executor] DRY-RUN buy plan prepared (no transaction broadcast).")
            return {**trade_plan, "status": "simulated"}

        if not quote:
            return {**trade_plan, "status": "quote_unavailable"}

        # Price impact guard
        price_impact = None
        try:
            price_impact = float((quote.get("priceImpactPct") or 0) * 100)
        except Exception:
            pass
        if price_impact is not None and price_impact > self.max_price_impact_pct:
            return {**trade_plan, "status": "blocked_price_impact", "price_impact_pct": price_impact}

        tx_sig = self._execute_swap_with_retry(quote, reverse=False)
        result = {**trade_plan, "status": "sent" if tx_sig else "failed", "tx_signature": tx_sig}
        if tx_sig and token_data and not self.dry_run:
            in_amount_sol = float(quote.get("inAmount", 0)) / 1_000_000_000 if quote else buy_size
            self._positions[cluster["token_address"]] = {
                "in_amount": in_amount_sol,
                "entry_price": token_data.get("price_usd"),
                "entry_sig": tx_sig,
                "symbol": token_data.get("symbol"),
            }
            self._log_json(
                self.positions_log,
                {
                    "ts": time.time(),
                    "token": cluster["token_address"],
                    "symbol": token_data.get("symbol"),
                    "in_amount_sol": in_amount_sol,
                    "entry_price": token_data.get("price_usd"),
                    "tx": tx_sig,
                },
            )
            # Add to PositionManager if enabled
            if self.position_manager:
                try:
                    sol_price = self._approx_sol_usd()
                    entry_price = token_data.get("price_usd") or 0
                    entry_tokens = 0.0
                    if entry_price and sol_price:
                        entry_tokens = (in_amount_sol * sol_price) / entry_price
                    pos = self.position_manager.add_position(
                        token_mint=cluster["token_address"],
                        token_symbol=token_data.get("symbol") or cluster["token_address"][:6],
                        entry_signature=tx_sig,
                        entry_slot=0,
                        entry_price_usd=entry_price or 0,
                        entry_amount_sol=in_amount_sol,
                        entry_amount_tokens=entry_tokens,
                        source=cluster.get("cluster_type") or "auto",
                        source_details={"cluster_score": cluster.get("cluster_score")},
                        custom_exits=None,
                    )
                    metrics_collector.position_set(cluster["token_address"], in_amount_sol)
                    print(f"[Positions] Tracked {pos.id} ({cluster['token_address'][:6]}...) size {in_amount_sol:.4f} SOL")
                except Exception as e:
                    print(f"[Positions] Failed to add position: {e}")
            else:
                self._start_exit_watch(cluster["token_address"], token_data.get("price_usd"))
        return result

    def flatten_positions(self):
        """
        Sell all known positions (best-effort).
        """
        if self.position_manager:
            try:
                for pos in self.position_manager.get_open_positions():
                    print(f"[Flatten] Closing {pos.token_mint[:6]}... via PositionManager")
                    self._run_coro(self.position_manager.close_position(pos.id, ExitReason.MANUAL))
                return
            except Exception as e:
                print(f"[Flatten] PositionManager flatten error: {e}")
        tokens = list(self._positions.keys())
        for token in tokens:
            amt = self._positions[token].get("in_amount", self.default_buy_sol)
            print(f"[Flatten] Selling {amt} SOL worth of {token}")
            self.execute_sell(token, amt)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _get_quote_full(self, token_mint: str, reverse: bool = False, amount_sol: Optional[float] = None, slippage_bps: int = 500, priority_fee: int = 0) -> Optional[Dict[str, Any]]:
        """
        Fetch a full Jupiter quote (raw response) for swap construction.
        If reverse=True, we quote token->SOL using SOL as output mint.
        """
        try:
            in_mint = token_mint if reverse else SOL_MINT
            out_mint = SOL_MINT if reverse else token_mint
            amount = amount_sol if amount_sol is not None else self.default_buy_sol
            in_amount_lamports = int(amount * 1_000_000_000)

            params = {
                "inputMint": in_mint,
                "outputMint": out_mint,
                "amount": in_amount_lamports,
                "slippageBps": slippage_bps,
                "platformFeeBps": 0,
                "computeUnitPriceMicroLamports": priority_fee,
                "onlyDirectRoutes": self.direct_route_only,
                "swapMode": "ExactIn",
            }
            if self.dex_preference:
                params["dexes"] = ",".join(self.dex_preference)

            resp = requests.get("https://quote-api.jup.ag/v6/quote", params=params, timeout=self.request_timeout)
            if resp.status_code != 200:
                print(f"[Executor] Jupiter quote error: {resp.status_code} {resp.text}")
                return None
            data = resp.json()
            # Enforce direct dex requirement: single hop and allowed dex
            if self.require_direct_dex:
                route_plan = data.get("routePlan") or []
                if len(route_plan) != 1:
                    return None
                dex_label = (route_plan[0].get("swapInfo", {}).get("label") or "").lower()
                if self.dex_preference:
                    allowed = any(dex_label.find(d) != -1 for d in self.dex_preference)
                    if not allowed:
                        return None
            return data
        except Exception as e:
            print(f"[Executor] Quote error: {e}")
            return None

    def _reset_daily_counters_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day_started:
            self._day_started = today
            self._trades_today = 0

    # ------------------------------------------------------------------ #
    # Swap execution
    # ------------------------------------------------------------------ #
    def _execute_swap_with_retry(self, quote: Dict[str, Any], reverse: bool = False, panic: bool = False) -> Optional[str]:
        """
        Build swap transaction from Jupiter and submit via Jito or RPC with retries,
        adaptive slippage and priority fee.
        """
        if not self.keypair or not self.public_key_str:
            print("[Executor] No keypair loaded; cannot broadcast.")
            return None

        for attempt in range(self.max_swap_retries + 1):
            base_slip = self.panic_slippage_bps if panic else self.slippage_bps_base
            base_fee = self.panic_priority_fee if panic else self.priority_fee_microlamports
            slippage = min(base_slip + attempt * self.slippage_bps_step,
                           self.max_panic_slippage_bps_cap if panic else self.max_slippage_bps_cap)
            priority_fee = base_fee + attempt * self.priority_fee_step
            try:
                # Re-quote with updated slippage/priority
                token_mint = quote.get("outputMint") if not reverse else quote.get("inputMint")
                if not token_mint:
                    token_mint = quote.get("tokenAddress", "")
                fresh_quote = self._get_quote_full(
                    token_mint=token_mint,
                    reverse=reverse,
                    amount_sol=float(quote.get("inAmount", 0)) / 1_000_000_000 if quote.get("inAmount") else None,
                    slippage_bps=slippage,
                    priority_fee=priority_fee,
                )
                if not fresh_quote:
                    print(f"[Executor] Quote retry {attempt} failed.")
                    continue

                payload = {
                    "quoteResponse": fresh_quote,
                    "userPublicKey": self.public_key_str,
                    "wrapAndUnwrapSol": True,
                    "computeUnitPriceMicroLamports": priority_fee,
                    "asLegacyTransaction": False,
                }
                if self.compute_unit_limit > 0:
                    payload["computeUnitLimit"] = self.compute_unit_limit

                resp = requests.post("https://quote-api.jup.ag/v6/swap", json=payload, timeout=self.request_timeout)
                if resp.status_code != 200:
                    print(f"[Executor] Jupiter swap build error: {resp.status_code} {resp.text}")
                    continue

                swap_data = resp.json()
                tx_base64 = swap_data.get("swapTransaction")
                if not tx_base64:
                    print("[Executor] No swapTransaction field in response.")
                    continue

                tx_bytes = base64.b64decode(tx_base64)
                tx = VersionedTransaction.deserialize(tx_bytes)
                tx.sign([self.keypair])
                raw_tx = tx.serialize()

                # Send via Jito if enabled, else RPC failover
                tx_sig = self._send_transaction(raw_tx, panic=panic, aggressive=not reverse)
                if tx_sig:
                    print(f"[Executor] Swap submitted: {tx_sig} (attempt {attempt}, slippage {slippage} bps, fee {priority_fee})")
                    self._trades_today += 1
                # Log PnL entry for sells
                if reverse:
                    self._record_pnl(fresh_quote, tx_sig)
                    # Remove pause/flatten flags if any persisted
                    if self.pause_file and os.path.exists(self.pause_file):
                        os.remove(self.pause_file)
                    return tx_sig
                else:
                    print(f"[Executor] Submit failed on attempt {attempt}, retrying...")
            except Exception as e:
                print(f"[Executor] Swap execution error (attempt {attempt}): {e}")
                continue
        # All attempts failed; alert
        self._alert_failure(f"Swap failed after retries. reverse={reverse}")
        return None

    def _load_keypair_from_env(self) -> Optional[Keypair]:
        """
        Load a Keypair from WALLET_PRIVATE_KEY (base58-encoded secret key).
        """
        secret = os.getenv("WALLET_PRIVATE_KEY", "").strip()
        if not secret:
            path = os.getenv("WALLET_KEYPAIR_PATH", "").strip()
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    secret = f.read().strip()
        if not secret:
            return None
        try:
            secret_bytes = b58decode(secret)
            if len(secret_bytes) == 64:
                return Keypair.from_secret_key(secret_bytes)
            # Fallback: hex string
            try:
                secret_bytes_hex = bytes.fromhex(secret)
                if len(secret_bytes_hex) == 64:
                    return Keypair.from_secret_key(secret_bytes_hex)
            except Exception:
                pass
            print("[Executor] Invalid key length; expected 64-byte secret key.")
            return None
        except Exception as e:
            print(f"[Executor] Failed to load keypair: {e}")
            return None

    def _record_pnl(self, quote: Dict[str, Any], tx_sig: str):
        """
        Simple PnL logging based on stored entry and current quote.
        """
        try:
            token = quote.get("inputMint") or quote.get("outputMint")
            if not token or token not in self._positions:
                return
            pos = self._positions[token]
            entry_price = pos.get("entry_price")
            symbol = pos.get("symbol") or token[:6]
            out_amount = float(quote.get("outAmount", 0)) / 1_000_000_000 if quote.get("outAmount") else 0
            in_amount = pos.get("in_amount", 0)
            if entry_price and out_amount:
                # crude PnL in SOL terms
                pnl_sol = out_amount - in_amount
                print(f"[PnL] {symbol}: entry {in_amount:.4f} SOL -> exit {out_amount:.4f} SOL | PnL {pnl_sol:.4f} SOL | tx {tx_sig}")
                try:
                    metrics_collector.record_pnl(
                        token=token,
                        symbol=symbol,
                        in_amount_sol=in_amount,
                        out_amount_sol=out_amount,
                        pnl_sol=pnl_sol,
                        entry_price_usd=entry_price,
                        exit_price_usd=None,
                    )
                except Exception:
                    pass
                self._log_json(self.pnl_log, {
                    "ts": time.time(),
                    "token": token,
                    "symbol": symbol,
                    "in_amount_sol": in_amount,
                    "out_amount_sol": out_amount,
                    "pnl_sol": pnl_sol,
                    "tx": tx_sig,
                })
            self._positions.pop(token, None)
        except Exception as e:
            print(f"[PnL] Error recording PnL: {e}")

    def _on_position_exit(self, position: Position, reason: ExitReason):
        """
        Callback from PositionManager when an exit executes.
        """
        try:
            metrics_collector.position_remove(position.token_mint)
            exit_val_sol = (position.entry_amount_sol + (position.realized_pnl_sol or 0))
            try:
                metrics_collector.record_pnl(
                    token=position.token_mint,
                    symbol=position.token_symbol,
                    in_amount_sol=position.entry_amount_sol,
                    out_amount_sol=exit_val_sol,
                    pnl_sol=position.realized_pnl_sol or 0.0,
                    entry_price_usd=position.entry_price_usd,
                    exit_price_usd=position.exit_price_usd,
                )
            except Exception:
                pass
            if self.alert_chat_id and telegram_bot.enabled:
                msg = (
                    f"🏁 <b>Position Closed</b>\n\n"
                    f"<b>Token:</b> <code>{position.token_mint[:12]}...</code>\n"
                    f"<b>Reason:</b> {reason.value}\n"
                    f"<b>Entry:</b> {position.entry_amount_sol:.4f} SOL @ ${position.entry_price_usd:.4f}\n"
                    f"<b>Exit:</b> {exit_val_sol:.4f} SOL @ ${position.exit_price_usd or 0:.4f}\n"
                    f"<b>PnL:</b> {position.realized_pnl_sol or 0:.4f} SOL\n"
                )
                telegram_bot.send_message(self.alert_chat_id, msg)
        except Exception as e:
            print(f"[Positions] Exit callback error: {e}")

    def _pretrade_checks(self, token_address: str) -> bool:
        """
        Lightweight pre-trade sellability/risk checks.
        """
        try:
            # Quick RugCheck/GoPlus sellable flag (from risk_sources)
            from risk_sources import rugcheck_report, goplus_security
            rc = rugcheck_report(token_address)
            gp = goplus_security(token_address)
            if rc and isinstance(rc, dict):
                if rc.get("status", "").upper() in {"RUG", "SCAM"}:
                    print("[Pretrade] RugCheck flagged RUG/SCAM")
                    return False
            if gp and gp.get("result"):
                res = next(iter(gp["result"].values())) if isinstance(gp["result"], dict) else None
                if res:
                    if res.get("is_honeypot") == "1":
                        print("[Pretrade] GoPlus honeypot flagged")
                        return False
                    if res.get("trading_halted") == "1":
                        print("[Pretrade] GoPlus trading halted")
                        return False
                    # Authority checks
                    if self.require_renounce_mint and res.get("is_mint_authority") == "1":
                        print("[Pretrade] Mint authority still enabled, blocking.")
                        return False
                    if self.require_renounce_freeze and res.get("is_freeze_authority") == "1":
                        print("[Pretrade] Freeze authority still enabled, blocking.")
                        return False
                    holder_cnt = res.get("holder_count") or res.get("holders") or 0
                    if holder_cnt and holder_cnt < self.min_holders:
                        print(f"[Pretrade] Holder count {holder_cnt} < min {self.min_holders}")
                        return False
                    mc = res.get("mcap") or res.get("market_cap") or 0
                    if mc:
                        if self.min_fdv_usd > 0 and mc < self.min_fdv_usd:
                            print(f"[Pretrade] Market cap {mc} < min {self.min_fdv_usd}")
                            return False
                        if self.max_fdv_usd > 0 and mc > self.max_fdv_usd:
                            print(f"[Pretrade] Market cap {mc} > max {self.max_fdv_usd}")
                            return False
                    buy_tax = res.get("buy_tax") or res.get("buyTax")
                    sell_tax = res.get("sell_tax") or res.get("sellTax")
                    try:
                        if buy_tax is not None and float(buy_tax) > self.max_buy_tax_pct:
                            print(f"[Pretrade] Buy tax {buy_tax}% > max {self.max_buy_tax_pct}%")
                            return False
                        if sell_tax is not None and float(sell_tax) > self.max_sell_tax_pct:
                            print(f"[Pretrade] Sell tax {sell_tax}% > max {self.max_sell_tax_pct}%")
                            return False
                    except Exception:
                        pass
                    if not self.allow_proxy and res.get("is_proxy") == "1":
                        print("[Pretrade] Proxy contract blocked.")
                        return False
                    if self.require_renounce_owner and res.get("owner_address"):
                        if res.get("owner_renounced") == "1" or res.get("is_renounced") == "1":
                            pass
                        else:
                            print("[Pretrade] Owner not renounced; blocking.")
                            return False
            # Quick sell quote sanity (small size) to ensure route exists
            test_quote = self._get_quote_full(token_mint=token_address, reverse=True, amount_sol=0.01, slippage_bps=800, priority_fee=self.priority_fee_microlamports)
            if not test_quote or not test_quote.get("outAmount"):
                print("[Pretrade] Sell test quote unavailable")
                return False
            # Optional Jupiter simulation for buy path
            if not self._simulate_swap(token_address, amount_sol=0.01, slippage_bps=self.slippage_bps_base):
                print("[Pretrade] Simulation failed")
                return False
            return True
        except Exception as e:
            print(f"[Pretrade] Error: {e}")
            return True

    # ------------------------------------------------------------------ #
    # Exit management (TP/SL/Trailing)
    # ------------------------------------------------------------------ #
    def _start_exit_watch(self, token_address: str, entry_price_usd: Optional[float]):
        if entry_price_usd is None or entry_price_usd <= 0:
            return
        if token_address in self._active_positions:
            return
        self._active_positions[token_address] = {
            "entry": entry_price_usd,
            "peak_pct": 0.0,
            "started": time.time(),
        }
        t = threading.Thread(target=self._watch_position, args=(token_address,), daemon=True)
        t.start()

    def _watch_position(self, token_address: str):
        try:
            info = self._active_positions.get(token_address)
            if not info:
                return
            entry = info["entry"]
            peak_pct = 0.0
            start_ts = info["started"]

            while True:
                # Timeout
                if time.time() - start_ts > self.exit_timeout_minutes * 60:
                    print(f"[Exit] Timeout reached for {token_address}, selling.")
                    self.execute_sell(token_address, self.default_buy_sol)
                    break

                td = dexscreener.get_token_data("solana", token_address)
                price = td.get("price_usd") if td else None
                if price:
                    change_pct = ((price - entry) / entry) * 100
                    peak_pct = max(peak_pct, change_pct)

                    # Stop loss
                    if change_pct <= -self.stop_loss_pct:
                        print(f"[Exit] Stop loss hit {change_pct:.2f}% for {token_address}")
                        self.execute_sell(token_address, self.default_buy_sol)
                        break

                    # Take profit
                    if change_pct >= self.take_profit_pct:
                        print(f"[Exit] Take profit hit {change_pct:.2f}% for {token_address}")
                        self.execute_sell(token_address, self.default_buy_sol)
                        break

                    # Trailing stop
                    if peak_pct >= self.trailing_activation_pct:
                        if (peak_pct - change_pct) >= self.trailing_stop_pct:
                            print(f"[Exit] Trailing stop triggered peak {peak_pct:.2f}% current {change_pct:.2f}% for {token_address}")
                            self.execute_sell(token_address, self.default_buy_sol)
                            break

                time.sleep(self.price_poll_seconds)
        except Exception as e:
            print(f"[Exit] Watch error for {token_address}: {e}")
        finally:
            self._active_positions.pop(token_address, None)

    # ------------------------------------------------------------------ #
        # RPC / Jito helpers
    # ------------------------------------------------------------------ #
    def _build_rpc_clients(self) -> List[Client]:
        rpc_list = []
        primary = os.getenv("SOLANA_RPC_URL")
        if primary:
            rpc_list.append(primary)
        fallback = os.getenv("FALLBACK_RPCS", "")
        for url in [u.strip() for u in fallback.split(",") if u.strip()]:
            rpc_list.append(url)
        # Also accept numbered FALLBACK_RPC_1..5
        for i in range(1, 6):
            alt = os.getenv(f"FALLBACK_RPC_{i}", "").strip()
            if alt:
                rpc_list.append(alt)
        if not rpc_list:
            rpc_list.append("https://api.mainnet-beta.solana.com")
        return [Client(url) for url in rpc_list]

    def _send_transaction(self, raw_tx: bytes, panic: bool = False, aggressive: bool = False) -> Optional[str]:
        # Jito first if enabled
        if self.enable_jito:
            sig = self._send_via_jito(raw_tx, panic=panic, aggressive=aggressive)
            if sig:
                return sig
        # RPC failover
        for client in self.rpc_clients:
            try:
                resp_send = client.send_raw_transaction(raw_tx, opts=TxOpts(skip_preflight=self.skip_preflight, preflight_commitment="confirmed"))
                tx_sig = resp_send.get("result") or resp_send.get("value") or resp_send
                return tx_sig
            except Exception as e:
                print(f"[Executor] RPC send failed on {client.endpoint_uri}: {e}")
                continue
        return None

    def _send_raw_transaction_bytes(self, raw_tx: bytes) -> Optional[str]:
        for client in self.rpc_clients:
            try:
                resp_send = client.send_raw_transaction(raw_tx, opts=TxOpts(skip_preflight=self.skip_preflight, preflight_commitment="confirmed"))
                tx_sig = resp_send.get("result") or resp_send.get("value") or resp_send
                return tx_sig
            except Exception as e:
                print(f"[Executor] RPC send failed on {client.endpoint_uri}: {e}")
                continue
        return None

    def _send_via_jito(self, raw_tx: bytes, panic: bool = False, aggressive: bool = False) -> Optional[str]:
        try:
            tx_b64 = base64.b64encode(raw_tx).decode()
            tip = self._current_jito_tip(panic=panic, aggressive=aggressive)
            payload = {
                "bundle": {
                    "transactions": [tx_b64],
                    "tip": tip,
                }
            }
            resp = requests.post(self.jito_url, json=payload, timeout=self.request_timeout)
            if resp.status_code != 200:
                print(f"[Executor] Jito bundle error: {resp.status_code} {resp.text}")
                return None
            data = resp.json()
            return data.get("bundleId") or data.get("result")
        except Exception as e:
            print(f"[Executor] Jito send error: {e}")
            return None

    def _alert_failure(self, message: str):
        chat = os.getenv("TELEGRAM_ALERT_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))
        if not chat or not telegram_bot.enabled:
            return
        try:
            telegram_bot.send_message(chat, f"⚠️ Auto-trade failure: {message}")
        except Exception:
            pass

    def _log_json(self, path: str, obj: Dict[str, Any]):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj) + "\n")
        except Exception as e:
            print(f"[Log] Failed to write {path}: {e}")

    def _simulate_swap(self, token_address: str, amount_sol: float, slippage_bps: int) -> bool:
        """
        Lightweight Jupiter swap simulation (quote + swap sim).
        """
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": token_address,
                "amount": int(amount_sol * 1_000_000_000),
                "slippageBps": slippage_bps,
                "platformFeeBps": 0,
            }
            quote_resp = requests.get("https://quote-api.jup.ag/v6/quote", params=params, timeout=self.request_timeout)
            if quote_resp.status_code != 200:
                return False
            quote = quote_resp.json()
            payload = {
                "quoteResponse": quote,
                "userPublicKey": self.public_key_str,
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": self.priority_fee_microlamports,
                "asLegacyTransaction": False,
                "simulate": True,
            }
            sim_resp = requests.post("https://quote-api.jup.ag/v6/swap", json=payload, timeout=self.request_timeout)
            if sim_resp.status_code != 200:
                print(f"[Sim] Swap sim error: {sim_resp.status_code} {sim_resp.text}")
                return False
            sim_data = sim_resp.json()
            if sim_data.get("error"):
                print(f"[Sim] Swap sim returned error: {sim_data.get('error')}")
                return False
            return True
        except Exception as e:
            print(f"[Sim] Exception: {e}")
            return False

    def _run_coro(self, coro):
        """
        Run a coroutine on the internal event loop and return result synchronously.
        """
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return fut.result()
        except Exception as e:
            print(f"[Async] Error running coroutine: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Metrics / Alerts helpers
    # ------------------------------------------------------------------ #
    def _init_metrics(self, token_mint: str, cluster: Dict[str, Any], start_ts: float) -> Optional[TradeMetrics]:
        if not self.enable_metrics:
            return None
        try:
            return TradeMetrics(
                trade_id=f"{token_mint[:6]}-{int(start_ts * 1000)}",
                token_mint=token_mint,
                timestamp=start_ts,
                cluster_id=cluster.get("cluster_id") if isinstance(cluster.get("cluster_id"), int) else None,
                cluster_type=cluster.get("cluster_type"),
                cluster_score=int(cluster.get("cluster_score", 0) or 0),
                requested_amount_sol=0.0,
                priority_fee_used=self.priority_fee_microlamports,
            )
        except Exception:
            return None

    def _post_trade_alerts_and_metrics(self, metrics: Optional[TradeMetrics], token_data: Optional[Dict[str, Any]]):
        if not metrics:
            return
        if metrics.total_latency_ms <= 0:
            metrics.total_latency_ms = (time.time() - metrics.timestamp) * 1000
        if self.enable_metrics:
            metrics_collector.record(metrics)

        if not self.alert_chat_id or not telegram_bot.enabled:
            return

        if metrics.success and self.alert_on_success and metrics.signature:
            telegram_bot.send_trade_executed(
                self.alert_chat_id,
                metrics.token_mint,
                metrics.actual_amount_sol,
                metrics.signature,
                metrics.total_latency_ms,
                metrics.cluster_score,
                token_data,
            )
            if self.alert_on_slow_trade and metrics.total_latency_ms > self.slow_trade_threshold_ms:
                slow_msg = telegram_bot.format_slow_trade(
                    metrics.token_mint, metrics.total_latency_ms, self.slow_trade_threshold_ms
                )
                telegram_bot.send_message(self.alert_chat_id, slow_msg)
        elif not metrics.success and self.alert_on_failure:
            telegram_bot.send_trade_failed(
                self.alert_chat_id,
                metrics.token_mint,
                metrics.error_message or metrics.error_type or "unknown",
                metrics.attempts,
                token_data,
            )

    # ------------------------------------------------------------------ #
        # Exposure and PnL tracking
    # ------------------------------------------------------------------ #
    def _exposure_allows(self, token_data: Optional[Dict[str, Any]]) -> bool:
        if not token_data:
            return True
        token = token_data.get("address") or token_data.get("token_address")
        if not token:
            return True
        per_token = self._positions.get(token, {})
        token_in_sol = per_token.get("in_amount", 0)
        if token_in_sol >= self.max_per_token_sol:
            print(f"[Exposure] Block: token exposure {token_in_sol} SOL >= {self.max_per_token_sol} SOL")
            return False
        global_sol = sum(p.get("in_amount", 0) for p in self._positions.values())
        if global_sol + self.default_buy_sol > self.max_global_sol:
            print(f"[Exposure] Block: global exposure would be {global_sol + self.default_buy_sol} SOL > {self.max_global_sol} SOL")
            return False
        return True

    def _determine_buy_size(self, token_data: Optional[Dict[str, Any]]) -> float:
        """
        If balance sizing is set (>0), use that % of current SOL balance, capped by per-token/global limits.
        """
        size = self.default_buy_sol
        if self.balance_sizing_pct > 0:
            bal = self._get_sol_balance()
            if bal is not None and bal > 0:
                candidate = bal * (self.balance_sizing_pct / 100.0)
                size = min(candidate, self.max_per_token_sol, self.max_global_sol)
        # Liquidity-relative cap
        if token_data and token_data.get("liquidity_usd"):
            liq = float(token_data["liquidity_usd"])
            # Approximate SOL value of liquidity assuming price_usd and SOL price ~100 USD (rough cap)
            price_usd = float(token_data.get("price_usd", 0) or 0)
            if price_usd > 0:
                # value of intended buy in USD
                buy_usd = size * self._approx_sol_usd()
                max_buy_usd = liq * (self.max_liq_pct_per_trade / 100.0)
                if buy_usd > max_buy_usd:
                    size = max(0.0001, size * (max_buy_usd / buy_usd))
                if self.max_trade_usd > 0 and buy_usd > self.max_trade_usd:
                    size = max(0.0001, size * (self.max_trade_usd / buy_usd))
        size = min(size, self.max_per_trade_sol)
        return max(size, 0.0001)

    def _get_sol_balance(self) -> Optional[float]:
        try:
            from solana.publickey import PublicKey
            pub = PublicKey(self.public_key_str)
            # Use first RPC client
            client = self.rpc_clients[0]
            resp = client.get_balance(pub)
            val = resp.get("result", {}).get("value") if isinstance(resp, dict) else getattr(resp, "value", None)
            if val is None:
                return None
            return val / 1_000_000_000
        except Exception as e:
            print(f"[Balance] Failed to fetch SOL balance: {e}")
            return None

    def _approx_sol_usd(self) -> float:
        """
        Lightweight SOL/USD approximation; use Jupiter price API for better accuracy.
        """
        try:
            resp = requests.get("https://price.jup.ag/v4/price?ids=SOL", timeout=self.request_timeout)
            if resp.status_code == 200:
                data = resp.json()
                price = data.get("data", {}).get("SOL", {}).get("price")
                if price:
                    return float(price)
        except Exception:
            pass
        if self.enable_pyth_price:
            pyth_price = self._pyth_sol_price()
            if pyth_price:
                return pyth_price
        return 100.0  # fallback heuristic

    def _pyth_sol_price(self) -> Optional[float]:
        """
        Fetch SOL/USD from Pyth Hermes (best-effort).
        """
        try:
            url = f"{self.pyth_endpoint}/v2/price_feeds"
            params = {"ids[]": self.pyth_sol_feed_id}
            resp = requests.get(url, params=params, timeout=self.request_timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not isinstance(data, list) or not data:
                return None
            price_info = data[0].get("price", {}) or {}
            price = price_info.get("price")
            expo = price_info.get("expo", -8)
            if price is None:
                return None
            return float(price) * (10 ** expo)
        except Exception:
            return None

    def _sentiment_ok(self, token_address: str) -> bool:
        """
        Simple sentiment gate: queries a free API with keywords; returns True if no data or score >= min.
        """
        if not self.sentiment_enabled or not self.sentiment_api:
            return True
        try:
            params = {
                "auth_token": self.sentiment_token,
                "currencies": "SOL",
                "filter": "news",
                "kind": "news",
                "public": "true",
            }
            resp = requests.get(self.sentiment_api, params=params, timeout=5)
            if resp.status_code != 200:
                return True  # fail open
            data = resp.json()
            posts = data.get("results") or data.get("data") or []
            score = 0.0
            hits = 0
            for p in posts:
                title = (p.get("title") or "").lower()
                if any(k.lower() in title for k in self.sentiment_keywords):
                    hits += 1
                    s = p.get("sentiment", 0) if isinstance(p, dict) else 0
                    try:
                        score += float(s)
                    except Exception:
                        pass
            if hits == 0:
                return True
            avg = score / max(1, hits)
            return avg >= self.min_sentiment_score
        except Exception:
            return True

    # ------------------------------------------------------------------ #
    # Direct AMM (Raydium/Orca) swap path (simplified placeholder)
    # ------------------------------------------------------------------ #
    def _direct_amm_swap(self, token_mint: str, amount_sol: float) -> Optional[str]:
        """
        Placeholder for direct AMM swap (Raydium/Orca) when Jupiter direct quote is unavailable.
        This uses Jupiter quote with direct-route enforcement first; if still unavailable, returns None.
        """
        try:
            quote = self._get_quote_full(token_mint=token_mint, slippage_bps=self.slippage_bps_base, priority_fee=self.priority_fee_microlamports, amount_sol=amount_sol)
            if not quote:
                return None
            # Build swap via Jupiter as a fallback; in a real direct AMM path, we'd construct the AMM IXs here.
            payload = {
                "quoteResponse": quote,
                "userPublicKey": self.public_key_str,
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": self.priority_fee_microlamports,
                "asLegacyTransaction": False,
            }
            if self.compute_unit_limit > 0:
                payload["computeUnitLimit"] = self.compute_unit_limit

            resp = requests.post("https://quote-api.jup.ag/v6/swap", json=payload, timeout=self.request_timeout)
            if resp.status_code != 200:
                print(f"[Direct] Swap build error: {resp.status_code} {resp.text}")
                return None

            swap_data = resp.json()
            tx_base64 = swap_data.get("swapTransaction")
            if not tx_base64:
                print("[Direct] No swapTransaction field in response.")
                return None

            tx_bytes = base64.b64decode(tx_base64)
            tx = VersionedTransaction.deserialize(tx_bytes)
            tx.sign([self.keypair])
            raw_tx = tx.serialize()
            sig = self._send_raw_transaction_bytes(raw_tx)
            if sig:
                print(f"[Direct] Swap submitted: {sig}")
            return sig
        except Exception as e:
            print(f"[Direct] AMM swap error: {e}")
            return None



# Global instance
trade_executor = TradeExecutor()


