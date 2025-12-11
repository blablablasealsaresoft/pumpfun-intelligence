"""
Ownership/upgradeability guardrails for SPL tokens.
Checks mint, freeze, and metadata update authorities to block ruggable tokens.
"""

from __future__ import annotations

import base64
import logging
import os
import struct
from dataclasses import dataclass
from typing import List, Optional

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# Known safe/burned addresses
BURNED_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}

# Program IDs
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
METAPLEX_PROGRAM = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")


@dataclass
class SafetyConfig:
    # What to enforce
    require_mint_renounced: bool = True
    require_freeze_renounced: bool = True
    require_metadata_immutable: bool = False  # Stricter, optional
    allow_token_2022: bool = False  # Token-2022 has extra features, riskier

    # Bypass for known tokens
    whitelist_mints: List[str] = None  # Skip checks for these

    def __post_init__(self):
        if self.whitelist_mints is None:
            self.whitelist_mints = []


@dataclass
class SafetyResult:
    is_safe: bool
    mint_authority: Optional[str]
    freeze_authority: Optional[str]
    token_program: str
    metadata_update_authority: Optional[str]
    warnings: List[str]

    # Detailed flags
    mint_renounced: bool = False
    freeze_renounced: bool = False
    metadata_immutable: bool = False
    is_token_2022: bool = False

    def to_dict(self) -> dict:
        return {
            "is_safe": self.is_safe,
            "mint_authority": self.mint_authority,
            "freeze_authority": self.freeze_authority,
            "mint_renounced": self.mint_renounced,
            "freeze_renounced": self.freeze_renounced,
            "is_token_2022": self.is_token_2022,
            "warnings": self.warnings,
        }


class TokenSafetyChecker:
    """
    Check token mint for ownership/upgradeability risks.
    """

    # SPL Token Mint layout (82 bytes)
    # https://github.com/solana-labs/solana-program-library/blob/master/token/program/src/state.rs
    MINT_LAYOUT_SIZE = 82

    def __init__(self, rpc_client: AsyncClient, config: Optional[SafetyConfig] = None):
        self.rpc = rpc_client
        self.config = config or SafetyConfig(
            require_mint_renounced=os.getenv("REQUIRE_MINT_RENOUNCED", "true").lower() == "true",
            require_freeze_renounced=os.getenv("REQUIRE_FREEZE_RENOUNCED", "true").lower() == "true",
            allow_token_2022=os.getenv("ALLOW_TOKEN_2022", "false").lower() == "true",
        )

        # Cache results (mint authorities don't change often once renounced)
        self._cache: dict[str, SafetyResult] = {}

    async def check_token(self, mint_address: str) -> SafetyResult:
        """
        Check if token mint is safe to trade.
        """
        # Check cache
        if mint_address in self._cache:
            return self._cache[mint_address]

        # Check whitelist
        if mint_address in self.config.whitelist_mints:
            result = SafetyResult(
                is_safe=True,
                mint_authority=None,
                freeze_authority=None,
                token_program="whitelisted",
                metadata_update_authority=None,
                warnings=[],
                mint_renounced=True,
                freeze_renounced=True,
            )
            self._cache[mint_address] = result
            return result

        warnings = []

        try:
            mint_pubkey = Pubkey.from_string(mint_address)

            # Fetch mint account
            resp = await self.rpc.get_account_info(mint_pubkey, encoding="base64")
            account_info = self._extract_account_info(resp)

            if not account_info or not account_info.get("data"):
                return SafetyResult(
                    is_safe=False,
                    mint_authority=None,
                    freeze_authority=None,
                    token_program="unknown",
                    metadata_update_authority=None,
                    warnings=["Mint account not found"],
                )

            account_data: bytes = account_info["data"]
            owner = account_info.get("owner")

            # Determine token program
            is_token_2022 = owner == TOKEN_2022_PROGRAM
            token_program = "token-2022" if is_token_2022 else "spl-token"

            if is_token_2022 and not self.config.allow_token_2022:
                warnings.append("Token-2022 program (higher risk)")

            # Parse mint account
            mint_authority, freeze_authority = self._parse_mint_account(account_data)

            # Check mint authority
            mint_renounced = self._is_renounced(mint_authority)
            if not mint_renounced:
                warnings.append(f"Mint authority active: {mint_authority}")

            # Check freeze authority
            freeze_renounced = self._is_renounced(freeze_authority)
            if not freeze_renounced:
                warnings.append(f"Freeze authority active: {freeze_authority}")

            # Optional: Check metadata update authority
            metadata_authority = None
            metadata_immutable = True

            if self.config.require_metadata_immutable:
                metadata_authority, metadata_immutable = await self._check_metadata(mint_pubkey)
                if not metadata_immutable:
                    warnings.append(f"Metadata mutable: {metadata_authority}")

            # Determine overall safety
            is_safe = True

            if self.config.require_mint_renounced and not mint_renounced:
                is_safe = False

            if self.config.require_freeze_renounced and not freeze_renounced:
                is_safe = False

            if is_token_2022 and not self.config.allow_token_2022:
                is_safe = False

            if self.config.require_metadata_immutable and not metadata_immutable:
                is_safe = False

            result = SafetyResult(
                is_safe=is_safe,
                mint_authority=mint_authority,
                freeze_authority=freeze_authority,
                token_program=token_program,
                metadata_update_authority=metadata_authority,
                warnings=warnings,
                mint_renounced=mint_renounced,
                freeze_renounced=freeze_renounced,
                metadata_immutable=metadata_immutable,
                is_token_2022=is_token_2022,
            )

            # Cache safe results (don't cache unsafe - might be temporary)
            if is_safe:
                self._cache[mint_address] = result

            return result

        except Exception as e:
            logger.error(f"Safety check failed for {mint_address}: {e}")
            return SafetyResult(
                is_safe=False,
                mint_authority=None,
                freeze_authority=None,
                token_program="error",
                metadata_update_authority=None,
                warnings=[f"Check failed: {str(e)}"],
            )

    def _parse_mint_account(self, data: bytes) -> tuple[Optional[str], Optional[str]]:
        """
        Parse SPL Token mint account to extract authorities.

        Mint layout (82 bytes):
        - mint_authority_option (4 bytes) + mint_authority (32 bytes)
        - supply (8 bytes)
        - decimals (1 byte)
        - is_initialized (1 byte)
        - freeze_authority_option (4 bytes) + freeze_authority (32 bytes)
        """
        if not data or len(data) < self.MINT_LAYOUT_SIZE:
            return None, None

        # Mint authority (offset 0)
        mint_auth_option = struct.unpack_from("<I", data, 0)[0]
        mint_authority = None
        if mint_auth_option == 1:
            mint_authority = str(Pubkey.from_bytes(data[4:36]))

        # Freeze authority (offset 46)
        freeze_auth_option = struct.unpack_from("<I", data, 46)[0]
        freeze_authority = None
        if freeze_auth_option == 1:
            freeze_authority = str(Pubkey.from_bytes(data[50:82]))

        return mint_authority, freeze_authority

    def _is_renounced(self, authority: Optional[str]) -> bool:
        """Check if authority is None or a burned address."""
        if authority is None:
            return True
        return authority in BURNED_ADDRESSES

    async def _check_metadata(self, mint: Pubkey) -> tuple[Optional[str], bool]:
        """
        Check Metaplex metadata for update authority.
        Returns (update_authority, is_immutable)
        """
        try:
            # Derive metadata PDA
            seeds = [
                b"metadata",
                bytes(METAPLEX_PROGRAM),
                bytes(mint),
            ]
            metadata_pda, _ = Pubkey.find_program_address(seeds, METAPLEX_PROGRAM)

            resp = await self.rpc.get_account_info(metadata_pda, encoding="base64")
            account_info = self._extract_account_info(resp)

            if not account_info or not account_info.get("data"):
                return None, True  # No metadata = immutable (can't update what doesn't exist)

            data = account_info["data"]

            # Metadata layout: update_authority at offset 1 (32 bytes)
            # is_mutable flag is near the end, varies by version
            if len(data) < 33:
                return None, True

            update_authority = str(Pubkey.from_bytes(data[1:33]))

            # Check if burned
            if self._is_renounced(update_authority):
                return update_authority, True

            return update_authority, False

        except Exception as e:
            logger.warning(f"Metadata check failed: {e}")
            return None, True  # Assume immutable on error (fail open for this check)

    def _extract_account_info(self, resp) -> Optional[dict]:
        """
        Normalize account info response from solana-py (dict or RPC object).
        """
        if not resp:
            return None

        value = None
        if hasattr(resp, "value"):
            value = getattr(resp, "value", None)
        elif isinstance(resp, dict):
            value = resp.get("result", {}).get("value")

        if not value:
            return None

        owner = getattr(value, "owner", None) if not isinstance(value, dict) else value.get("owner")
        data_field = getattr(value, "data", None) if not isinstance(value, dict) else value.get("data")

        owner_pubkey = None
        if isinstance(owner, Pubkey):
            owner_pubkey = owner
        elif isinstance(owner, str):
            try:
                owner_pubkey = Pubkey.from_string(owner)
            except Exception:
                owner_pubkey = None

        data_bytes: Optional[bytes] = None
        try:
            if isinstance(data_field, (bytes, bytearray)):
                data_bytes = bytes(data_field)
            elif isinstance(data_field, (list, tuple)) and data_field:
                raw = data_field[0]
                if isinstance(raw, str):
                    data_bytes = base64.b64decode(raw)
                elif isinstance(raw, (bytes, bytearray)):
                    data_bytes = bytes(raw)
            elif isinstance(data_field, str):
                data_bytes = base64.b64decode(data_field)
        except Exception:
            data_bytes = None

        return {"owner": owner_pubkey, "data": data_bytes}

    def clear_cache(self):
        """Clear the safety cache."""
        self._cache.clear()


# Convenience function
async def is_token_safe(
    rpc_client: AsyncClient,
    mint_address: str,
    require_mint_renounced: bool = True,
    require_freeze_renounced: bool = True,
) -> tuple[bool, List[str]]:
    """
    Quick check if token is safe to trade.
    Returns (is_safe, warnings)
    """
    checker = TokenSafetyChecker(
        rpc_client,
        SafetyConfig(
            require_mint_renounced=require_mint_renounced,
            require_freeze_renounced=require_freeze_renounced,
        ),
    )
    result = await checker.check_token(mint_address)
    return result.is_safe, result.warnings

