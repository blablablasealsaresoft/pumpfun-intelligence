from dataclasses import dataclass
from typing import Optional, Callable
from time import time
import os
import logging

logger = logging.getLogger(__name__)


@dataclass
class PauseConfig:
    max_consecutive_failures: int = 3
    max_failures_per_hour: int = 10
    min_sol_balance_lamports: int = 50_000_000
    critical_sol_balance_lamports: int = 10_000_000
    failure_pause_duration_sec: int = 300
    balance_check_interval_sec: int = 60
    auto_resume_after_sec: int = 1800
    require_manual_resume_on_critical: bool = True


@dataclass
class PauseState:
    is_paused: bool = False
    pause_reason: Optional[str] = None
    pause_start: Optional[float] = None
    resume_at: Optional[float] = None
    requires_manual_resume: bool = False
    consecutive_failures: int = 0
    failures_this_hour: int = 0
    hour_start: float = 0.0
    last_balance_check: float = 0.0
    last_known_balance: int = 0


class AutoPauseManager:
    def __init__(
        self,
        rpc_client,
        wallet_pubkey,
        config: Optional[PauseConfig] = None,
        on_pause: Optional[Callable] = None,
        on_resume: Optional[Callable] = None,
    ):
        self.rpc = rpc_client
        self.wallet = wallet_pubkey
        self.config = config or PauseConfig(
            max_consecutive_failures=int(os.getenv("MAX_CONSECUTIVE_FAILURES", "3")),
            max_failures_per_hour=int(os.getenv("MAX_FAILURES_PER_HOUR", "10")),
            min_sol_balance_lamports=int(os.getenv("MIN_SOL_BALANCE_LAMPORTS", "50000000")),
            critical_sol_balance_lamports=int(os.getenv("CRITICAL_SOL_BALANCE_LAMPORTS", "10000000")),
            failure_pause_duration_sec=int(os.getenv("FAILURE_PAUSE_DURATION_SEC", "300")),
            balance_check_interval_sec=int(os.getenv("BALANCE_CHECK_INTERVAL_SEC", "60")),
            auto_resume_after_sec=int(os.getenv("AUTO_RESUME_AFTER_SEC", "1800")),
            require_manual_resume_on_critical=(
                os.getenv("REQUIRE_MANUAL_RESUME_ON_CRITICAL", "true").lower()
                in {"1", "true", "yes", "on"}
            ),
        )
        self.state = PauseState(hour_start=time())
        self.on_pause = on_pause
        self.on_resume = on_resume

    def is_trading_allowed(self) -> tuple[bool, Optional[str]]:
        if not self.state.is_paused:
            return True, None
        now = time()
        if self.state.resume_at and now >= self.state.resume_at and not self.state.requires_manual_resume:
            self._resume("auto_resume_timeout")
            return True, None
        return False, self.state.pause_reason

    def record_success(self):
        self.state.consecutive_failures = 0
        if self.state.is_paused and self.state.pause_reason == "consecutive_failures":
            logger.info("Trade succeeded while paused - consider manual resume")

    def record_failure(self, error_type: Optional[str] = None) -> bool:
        now = time()
        if now - self.state.hour_start > 3600:
            self.state.failures_this_hour = 0
            self.state.hour_start = now

        self.state.consecutive_failures += 1
        self.state.failures_this_hour += 1

        if self.state.consecutive_failures >= self.config.max_consecutive_failures:
            self._pause(
                reason="consecutive_failures",
                duration_sec=self.config.failure_pause_duration_sec,
                details=f"{self.state.consecutive_failures} consecutive failures, last: {error_type}",
            )
            return True

        if self.state.failures_this_hour >= self.config.max_failures_per_hour:
            self._pause(
                reason="hourly_failure_limit",
                duration_sec=self.config.failure_pause_duration_sec * 2,
                details=f"{self.state.failures_this_hour} failures in the last hour",
            )
            return True

        return False

    async def check_balance(self) -> tuple[int, bool]:
        now = time()
        if now - self.state.last_balance_check < self.config.balance_check_interval_sec:
            return self.state.last_known_balance, False
        try:
            resp = await self.rpc.get_balance(self.wallet)
            balance = getattr(resp, "value", None)
            self.state.last_known_balance = balance or 0
            self.state.last_balance_check = now

            if balance is None:
                return 0, False

            if balance < self.config.critical_sol_balance_lamports:
                self._pause(
                    reason="critical_balance",
                    duration_sec=None,
                    details=f"Balance critically low: {balance / 1e9:.4f} SOL",
                    require_manual=True,
                )
                return balance, True

            if balance < self.config.min_sol_balance_lamports:
                self._pause(
                    reason="low_balance",
                    duration_sec=self.config.auto_resume_after_sec,
                    details=f"Balance low: {balance / 1e9:.4f} SOL",
                )
                return balance, False

            return balance, False
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return self.state.last_known_balance, False

    def manual_resume(self) -> bool:
        if not self.state.is_paused:
            return False
        self._resume("manual")
        return True

    def manual_pause(self, reason: str = "manual", duration_sec: int = 3600):
        self._pause(reason=reason, duration_sec=duration_sec, details="Manual pause requested")

    def _pause(
        self,
        reason: str,
        duration_sec: Optional[int],
        details: str = "",
        require_manual: bool = False,
    ):
        now = time()
        self.state.is_paused = True
        self.state.pause_reason = reason
        self.state.pause_start = now
        self.state.resume_at = now + duration_sec if duration_sec else None
        self.state.requires_manual_resume = require_manual or self.config.require_manual_resume_on_critical

        logger.warning(
            f"[AUTO-PAUSE] Trading paused: {reason} | {details} | "
            f"Resume: {'manual' if self.state.requires_manual_resume else duration_sec}"
        )
        if self.on_pause:
            self.on_pause(reason, details)

    def _resume(self, trigger: str = "manual"):
        self.state.is_paused = False
        self.state.pause_reason = None
        self.state.pause_start = None
        self.state.resume_at = None
        self.state.requires_manual_resume = False
        self.state.consecutive_failures = 0

        logger.info(f"[AUTO-PAUSE] Trading resumed: {trigger}")
        if self.on_resume:
            self.on_resume(trigger)

    def get_status(self) -> dict:
        return {
            "is_paused": self.state.is_paused,
            "pause_reason": self.state.pause_reason,
            "pause_duration_sec": (time() - self.state.pause_start) if self.state.pause_start else 0,
            "resume_at": self.state.resume_at,
            "requires_manual_resume": self.state.requires_manual_resume,
            "consecutive_failures": self.state.consecutive_failures,
            "failures_this_hour": self.state.failures_this_hour,
            "last_balance": self.state.last_known_balance,
        }

