"""
Enhanced Telegram Bot Service
Rich notifications with formatting, buttons, and real-time alerts
"""

import requests
import os
from typing import Optional, Dict, Any, List
from datetime import datetime

TELEGRAM_API_BASE = "https://api.telegram.org"

class TelegramBot:
    def __init__(self):
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.enabled = bool(self.bot_token)
        self.commands_enabled = True
        
        if not self.enabled:
            print("[Telegram] Bot token not configured - notifications disabled")
    
    def send_message(self, chat_id: str, text: str, parse_mode: str = 'HTML',
                     disable_preview: bool = True, reply_markup: Optional[Dict] = None) -> bool:
        """
        Send Telegram message
        """
        if not self.enabled:
            print(f"[Telegram] Would send: {text[:100]}...")
            return False
        
        try:
            url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
            
            payload = {
                'chat_id': chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'disable_web_page_preview': disable_preview
            }
            
            if reply_markup:
                payload['reply_markup'] = reply_markup
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code != 200:
                print(f"[Telegram] Error: {response.status_code} - {response.text}")
                return False
            
            return True
        
        except Exception as e:
            print(f"[Telegram] Error sending message: {e}")
            return False
    
    def format_cluster_alert(self, cluster: Dict[str, Any], token_data: Optional[Dict] = None) -> str:
        """
        Format cluster detection alert with rich formatting
        """
        emoji = self._get_cluster_emoji(cluster['cluster_type'])
        score = cluster['cluster_score'] / 100  # Convert to percentage
        volume = self._format_usd(cluster['total_volume_usd'])
        
        # Token info
        token_name = token_data.get('name', 'Unknown') if token_data else 'Unknown'
        token_symbol = token_data.get('symbol', '') if token_data else ''
        price_usd = token_data.get('price_usd', 0) if token_data else 0
        
        # Build message
        message = f"{emoji} <b>SMART MONEY CLUSTER DETECTED!</b>\n\n"
        
        if token_symbol:
            message += f"<b>Token:</b> ${token_symbol} ({token_name})\n"
        
        message += f"<b>Address:</b> <code>{cluster['token_address'][:8]}...{cluster['token_address'][-6:]}</code>\n"
        message += f"<b>Cluster Type:</b> {cluster['cluster_type'].replace('_', ' ').title()}\n"
        message += f"<b>Cluster Score:</b> {score:.1f}/100\n"
        message += f"<b>Wallets:</b> {cluster['wallet_count']}"
        
        if 'smart_money_count' in cluster and cluster['smart_money_count'] > 0:
            message += f" ({cluster['smart_money_count']} smart money)\n"
        else:
            message += "\n"
        
        message += f"<b>Total Volume:</b> {volume}\n"
        
        if price_usd > 0:
            message += f"<b>Current Price:</b> ${price_usd:.8f}\n"
        
        message += f"<b>Detected:</b> {cluster['detected_at'].strftime('%H:%M:%S')}\n\n"
        
        # Add signal
        signal = cluster.get('signal', 'MONITOR')
        signal_emoji = self._get_signal_emoji(signal)
        message += f"{signal_emoji} <b>Signal:</b> {signal}\n\n"
        
        # Add recommendation
        if score >= 70:
            message += "⚡️ <i>Strong coordinated activity detected. Consider immediate action.</i>"
        elif score >= 50:
            message += "👀 <i>Significant wallet clustering. Monitor closely for entry.</i>"
        else:
            message += "📊 <i>Cluster detected. Track for potential movement.</i>"
        
        return message
    
    def format_graduation_alert(self, token_address: str, graduation_data: Dict[str, Any],
                                  token_data: Optional[Dict] = None) -> str:
        """
        Format token graduation alert (Pump.fun -> Raydium)
        THIS IS A CRITICAL PROFIT SIGNAL!
        """
        token_symbol = token_data.get('symbol', '') if token_data else ''
        token_name = token_data.get('name', 'Unknown') if token_data else 'Unknown'
        
        message = "🚀 <b>TOKEN GRADUATION DETECTED!</b>\n\n"
        
        if token_symbol:
            message += f"<b>Token:</b> ${token_symbol} ({token_name})\n"
        
        message += f"<b>Address:</b> <code>{token_address[:8]}...{token_address[-6:]}</code>\n"
        message += f"<b>Status:</b> Graduated to {graduation_data.get('dex', 'DEX')}\n"
        
        if 'liquidity_usd' in graduation_data:
            message += f"<b>Liquidity:</b> {self._format_usd(int(graduation_data['liquidity_usd'] * 100))}\n"
        
        if 'volume_24h' in graduation_data:
            message += f"<b>Volume 24h:</b> {self._format_usd(int(graduation_data['volume_24h'] * 100))}\n"
        
        if 'price_usd' in graduation_data:
            message += f"<b>Price:</b> ${graduation_data['price_usd']:.8f}\n"
        
        if 'graduation_time' in graduation_data and graduation_data['graduation_time']:
            message += f"<b>Graduated:</b> {graduation_data['graduation_time'].strftime('%H:%M:%S')}\n"
        
        message += "\n"
        
        signal = graduation_data.get('signal', 'MONITOR')
        if signal == 'STRONG_BUY':
            message += "🎯 <b>Signal: STRONG BUY</b>\n\n"
            message += "💰 <i>Historical data shows 2-5x pumps within 24-48 hours after graduation. Consider immediate entry!</i>"
        else:
            message += "👀 <b>Signal: MONITOR</b>\n\n"
            message += "📊 <i>Token graduated but volume is low. Wait for volume confirmation before entry.</i>"
        
        return message
    
    def format_rug_pull_warning(self, token_address: str, risk_data: Dict[str, Any],
                                  token_data: Optional[Dict] = None) -> str:
        """
        Format rug pull warning alert
        """
        token_symbol = token_data.get('symbol', '') if token_data else ''
        token_name = token_data.get('name', 'Unknown') if token_data else 'Unknown'
        
        risk_level = risk_data.get('risk', 'UNKNOWN')
        
        emoji_map = {
            'CRITICAL': '🚨',
            'HIGH': '⚠️',
            'MEDIUM': '⚡️',
            'LOW': '✅'
        }
        
        emoji = emoji_map.get(risk_level, '❓')
        
        message = f"{emoji} <b>RUG PULL RISK ALERT</b>\n\n"
        
        if token_symbol:
            message += f"<b>Token:</b> ${token_symbol} ({token_name})\n"
        
        message += f"<b>Address:</b> <code>{token_address[:8]}...{token_address[-6:]}</code>\n"
        message += f"<b>Risk Level:</b> {risk_level}\n"
        message += f"<b>Reason:</b> {risk_data.get('reason', 'Unknown')}\n"
        
        if 'liquidity_usd' in risk_data:
            message += f"<b>Liquidity:</b> {self._format_usd(int(risk_data['liquidity_usd'] * 100))}\n"
        
        message += "\n"
        
        recommendation = risk_data.get('recommendation', 'MONITOR')
        if recommendation == 'DO_NOT_BUY':
            message += "🛑 <b>RECOMMENDATION: DO NOT BUY</b>\n\n"
            message += "❌ <i>Extremely high risk. Avoid this token!</i>"
        elif recommendation == 'EXTREME_CAUTION':
            message += "⚠️ <b>RECOMMENDATION: EXTREME CAUTION</b>\n\n"
            message += "⚡️ <i>High risk. Only trade with money you can afford to lose.</i>"
        else:
            message += "👀 <b>RECOMMENDATION: MONITOR CLOSELY</b>\n\n"
            message += "📊 <i>Watch for liquidity changes before trading.</i>"
        
        return message

    # ------------------------------------------------------------------ #
    # Trading alerts (Item #5)
    # ------------------------------------------------------------------ #
    def format_trade_executed(
        self,
        token_mint: str,
        amount_sol: float,
        signature: str,
        latency_ms: float,
        cluster_score: int = 0,
        token_data: Optional[Dict] = None,
    ) -> str:
        """Format successful trade alert."""
        token_symbol = token_data.get("symbol", "") if token_data else ""

        message = "✅ <b>TRADE EXECUTED</b>\n\n"
        if token_symbol:
            message += f"<b>Token:</b> ${token_symbol}\n"
        message += f"<b>Address:</b> <code>{token_mint[:8]}...{token_mint[-6:]}</code>\n"
        message += f"<b>Amount:</b> {amount_sol:.4f} SOL\n"
        message += f"<b>Latency:</b> {latency_ms:.0f}ms\n"
        if cluster_score > 0:
            message += f"<b>Cluster Score:</b> {cluster_score/100:.0f}/100\n"
        message += f"<b>TX:</b> <a href='https://solscan.io/tx/{signature}'>View on Solscan</a>"
        return message

    def format_trade_failed(
        self,
        token_mint: str,
        error: str,
        attempts: int,
        token_data: Optional[Dict] = None,
    ) -> str:
        """Format failed trade alert."""
        token_symbol = token_data.get("symbol", "") if token_data else ""

        message = "❌ <b>TRADE FAILED</b>\n\n"
        if token_symbol:
            message += f"<b>Token:</b> ${token_symbol}\n"
        message += f"<b>Address:</b> <code>{token_mint[:8]}...{token_mint[-6:]}</code>\n"
        message += f"<b>Error:</b> {error[:100]}\n"
        message += f"<b>Attempts:</b> {attempts}"
        return message

    def format_trading_paused(self, reason: str, details: str) -> str:
        """Format trading paused alert."""
        message = "🔴 <b>TRADING PAUSED</b>\n\n"
        message += f"<b>Reason:</b> {reason}\n"
        message += f"<b>Details:</b> {details}\n\n"
        message += "⚠️ <i>Manual intervention may be required.</i>"
        return message

    def format_trading_resumed(self, trigger: str) -> str:
        """Format trading resumed alert."""
        return f"🟢 <b>TRADING RESUMED</b>\n\n<b>Trigger:</b> {trigger}"

    def format_safety_blocked(
        self,
        token_mint: str,
        warnings: List[str],
        token_data: Optional[Dict] = None,
    ) -> str:
        """Format token blocked by safety checks."""
        token_symbol = token_data.get("symbol", "") if token_data else ""

        message = "🛡️ <b>TOKEN BLOCKED (SAFETY)</b>\n\n"
        if token_symbol:
            message += f"<b>Token:</b> ${token_symbol}\n"
        message += f"<b>Address:</b> <code>{token_mint[:8]}...{token_mint[-6:]}</code>\n"
        message += "<b>Warnings:</b>\n"
        for w in warnings[:5]:
            message += f"• {w}\n"
        return message

    def format_low_balance(self, balance_sol: float, threshold_sol: float) -> str:
        """Format low balance warning."""
        message = "⚠️ <b>LOW BALANCE WARNING</b>\n\n"
        message += f"<b>Current:</b> {balance_sol:.4f} SOL\n"
        message += f"<b>Threshold:</b> {threshold_sol:.4f} SOL\n\n"
        message += "💡 <i>Trading may pause soon.</i>"
        return message

    def format_slow_trade(self, token_mint: str, latency_ms: float, threshold_ms: float) -> str:
        """Format slow trade warning."""
        message = "🐢 <b>SLOW TRADE</b>\n\n"
        message += f"<b>Token:</b> <code>{token_mint[:8]}...</code>\n"
        message += f"<b>Latency:</b> {latency_ms:.0f}ms (threshold: {threshold_ms:.0f}ms)"
        return message

    def format_daily_summary(self, stats: Dict[str, Any]) -> str:
        """Format daily trading summary."""
        message = "📊 <b>DAILY SUMMARY</b>\n\n"
        message += f"<b>Total Trades:</b> {stats.get('total_trades', 0)}\n"
        message += f"<b>Successful:</b> {stats.get('successful', 0)}\n"
        message += f"<b>Failed:</b> {stats.get('failed', 0)}\n"
        message += f"<b>Success Rate:</b> {stats.get('success_rate', 0):.1%}\n"
        message += f"<b>Avg Latency:</b> {stats.get('avg_latency_ms', 0):.0f}ms\n"
        message += f"<b>Clusters Traded:</b> {stats.get('clusters_traded', 0)}"
        return message

    # Convenience senders
    def send_trade_executed(
        self,
        chat_id: str,
        token_mint: str,
        amount_sol: float,
        signature: str,
        latency_ms: float,
        cluster_score: int = 0,
        token_data: Optional[Dict] = None,
    ) -> bool:
        message = self.format_trade_executed(
            token_mint, amount_sol, signature, latency_ms, cluster_score, token_data
        )
        buttons = {
            "inline_keyboard": [
                [
                    {"text": "📊 DexScreener", "url": f"https://dexscreener.com/solana/{token_mint}"},
                    {"text": "🔍 Solscan", "url": f"https://solscan.io/tx/{signature}"},
                ]
            ]
        }
        return self.send_message(chat_id, message, reply_markup=buttons)

    def send_trade_failed(
        self,
        chat_id: str,
        token_mint: str,
        error: str,
        attempts: int,
        token_data: Optional[Dict] = None,
    ) -> bool:
        message = self.format_trade_failed(token_mint, error, attempts, token_data)
        return self.send_message(chat_id, message)

    def send_trading_paused(self, chat_id: str, reason: str, details: str) -> bool:
        return self.send_message(chat_id, self.format_trading_paused(reason, details))

    def send_trading_resumed(self, chat_id: str, trigger: str) -> bool:
        return self.send_message(chat_id, self.format_trading_resumed(trigger))

    def send_safety_blocked(
        self,
        chat_id: str,
        token_mint: str,
        warnings: List[str],
        token_data: Optional[Dict] = None,
    ) -> bool:
        return self.send_message(chat_id, self.format_safety_blocked(token_mint, warnings, token_data))

    def send_low_balance(self, chat_id: str, balance_sol: float, threshold_sol: float) -> bool:
        return self.send_message(chat_id, self.format_low_balance(balance_sol, threshold_sol))

    def send_daily_summary(self, chat_id: str, stats: Dict[str, Any]) -> bool:
        return self.send_message(chat_id, self.format_daily_summary(stats))
    
    def format_smart_money_alert(self, wallet_address: str, wallet_data: Dict[str, Any],
                                   action: str, token_address: str, amount_usd: float) -> str:
        """
        Format smart money wallet activity alert
        """
        win_rate = wallet_data.get('win_rate', 0) / 100
        total_trades = wallet_data.get('total_trades', 0)
        
        action_emoji = '🟢' if action == 'buy' else '🔴'
        
        message = f"{action_emoji} <b>SMART MONEY {action.upper()}</b>\n\n"
        message += f"<b>Wallet:</b> <code>{wallet_address[:8]}...{wallet_address[-6:]}</code>\n"
        message += f"<b>Win Rate:</b> {win_rate:.1f}% ({total_trades} trades)\n"
        message += f"<b>Token:</b> <code>{token_address[:8]}...{token_address[-6:]}</code>\n"
        message += f"<b>Amount:</b> {self._format_usd(int(amount_usd * 100))}\n\n"
        
        if action == 'buy':
            message += "💡 <i>Profitable wallet is accumulating. Consider copy trading.</i>"
        else:
            message += "⚠️ <i>Profitable wallet is exiting. Consider taking profits.</i>"
        
        return message
    
    def send_cluster_alert(self, chat_id: str, cluster: Dict[str, Any],
                            token_data: Optional[Dict] = None) -> bool:
        """
        Send cluster detection alert with inline buttons
        """
        message = self.format_cluster_alert(cluster, token_data)
        
        # Add inline buttons
        buttons = {
            'inline_keyboard': [[
                {'text': '📊 View on DexScreener', 'url': f"https://dexscreener.com/solana/{cluster['token_address']}"},
                {'text': '🔍 Track Token', 'callback_data': f"track_{cluster['token_address']}"}
            ]]
        }
        
        return self.send_message(chat_id, message, reply_markup=buttons)

    # ------------------------------------------------------------------ #
    # Command handling (minimal)
    # ------------------------------------------------------------------ #
    def fetch_updates(self, offset: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/getUpdates"
            params = {"timeout": 0}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("result", [])
        except Exception as e:
            print(f"[Telegram] fetch_updates error: {e}")
            return []

    def send_plain(self, chat_id: str, text: str) -> None:
        self.send_message(chat_id, text, parse_mode="HTML", disable_preview=True)
    
    def send_graduation_alert(self, chat_id: str, token_address: str,
                               graduation_data: Dict[str, Any], token_data: Optional[Dict] = None) -> bool:
        """
        Send graduation alert
        """
        message = self.format_graduation_alert(token_address, graduation_data, token_data)
        
        buttons = {
            'inline_keyboard': [[
                {'text': '🚀 View on Raydium', 'url': f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={token_address}"},
                {'text': '📊 DexScreener', 'url': f"https://dexscreener.com/solana/{token_address}"}
            ]]
        }
        
        return self.send_message(chat_id, message, reply_markup=buttons)
    
    def _get_cluster_emoji(self, cluster_type: str) -> str:
        """Get emoji for cluster type"""
        emoji_map = {
            'temporal': '⚡',
            'smart_money': '🧠',
            'early_accumulation': '🌱'
        }
        return emoji_map.get(cluster_type, '📊')
    
    def _get_signal_emoji(self, signal: str) -> str:
        """Get emoji for signal"""
        emoji_map = {
            'STRONG_BUY': '🎯',
            'BUY': '🟢',
            'ACCUMULATE': '📈',
            'MONITOR': '👀',
            'WAIT': '⏸️'
        }
        return emoji_map.get(signal, '📊')
    
    def _format_usd(self, cents: int) -> str:
        """Format USD amount from cents"""
        return f"${cents / 100:,.2f}"

# Global instance
telegram_bot = TelegramBot()

