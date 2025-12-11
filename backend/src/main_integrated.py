"""
Pump.fun Intelligence Platform - Integrated Version
Complete with database, clustering, DexScreener, and Telegram
"""

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import os
from datetime import datetime

# Import all services
import database as db
from dexscreener_api import dexscreener
from clustering_service import cluster_detector
from telegram_service import telegram_bot
import solana_api
from trading import metrics_collector

app = Flask(__name__)
CORS(app)

# Initialize database
db.init_database()

# Health check
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'features': {
            'database': True,
            'dexscreener': True,
            'clustering': True,
            'telegram': telegram_bot.enabled
        }
    })

# Basic wallet info
@app.route('/api/wallet/<public_key>', methods=['GET'])
def get_wallet_info(public_key):
    try:
        # Get Solana data
        balance = solana_api.get_balance(public_key)
        transactions = solana_api.get_transaction_history(public_key)
        
        # Check database for stored wallet data
        wallet_db = db.get_wallet(public_key)
        
        response = {
            'address': public_key,
            'balance_sol': balance,
            'transactions': transactions[:10],  # Last 10
            'database_info': wallet_db
        }
        
        return jsonify(response)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Token analysis with DexScreener
@app.route('/api/token/<token_address>', methods=['GET'])
def get_token_info(token_address):
    try:
        # Get DexScreener data
        token_data = dexscreener.get_token_data('solana', token_address)
        
        if not token_data:
            return jsonify({'error': 'Token not found'}), 404
        
        # Check graduation status
        graduation = dexscreener.check_graduation_status(token_address)
        
        # Analyze liquidity risk
        liquidity_risk = dexscreener.analyze_liquidity_changes(token_address)
        
        # Get holder distribution analysis
        holder_dist = dexscreener.get_holder_distribution(token_address)
        
        # Store in database
        db.upsert_token(
            address=token_address,
            symbol=token_data['symbol'],
            name=token_data['name'],
            price_usd=int(token_data['price_usd'] * 100),
            liquidity_usd=int(token_data['liquidity_usd'] * 100),
            volume_24h=int(token_data['volume_24h'] * 100),
            unique_wallets_24h=token_data['unique_wallets_24h']
        )
        
        response = {
            'token': token_data,
            'graduation': graduation,
            'liquidity_risk': liquidity_risk,
            'holder_distribution': holder_dist
        }
        
        return jsonify(response)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Cluster detection
@app.route('/api/clusters/detect', methods=['POST'])
def detect_clusters():
    try:
        data = request.json or {}
        hours = data.get('hours', 1)
        
        # Run all cluster detection algorithms
        all_clusters = cluster_detector.detect_all_clusters(hours)
        
        # Save high-score clusters to database
        saved_clusters = []
        for cluster_type, clusters in all_clusters.items():
            for cluster in clusters:
                if cluster['cluster_score'] >= 5000:  # Only save significant clusters
                    cluster_id = cluster_detector.save_cluster_to_db(cluster)
                    cluster['id'] = cluster_id
                    saved_clusters.append(cluster)
        
        return jsonify({
            'clusters': all_clusters,
            'saved_count': len(saved_clusters),
            'high_score_clusters': saved_clusters
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Get active clusters
@app.route('/api/clusters/active', methods=['GET'])
def get_active_clusters():
    try:
        clusters = db.get_active_clusters()
        
        # Enrich with current token data
        enriched = []
        for cluster in clusters:
            token_data = dexscreener.get_token_data('solana', cluster['token_address'])
            
            enriched_cluster = dict(cluster)
            enriched_cluster['token_data'] = token_data
            enriched.append(enriched_cluster)
        
        return jsonify({
            'clusters': enriched,
            'count': len(enriched)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route("/metrics", methods=["GET"])
def prometheus_metrics():
    """
    Minimal Prometheus-style metrics endpoint.
    """
    stats = metrics_collector.get_stats()
    lines = [
        f"trade_total {stats.get('total_trades', 0)}",
        f"trade_success {stats.get('successful', 0)}",
        f"trade_failed {stats.get('failed', 0)}",
        f"trade_success_rate {stats.get('success_rate', 0)}",
        f"trade_avg_latency_ms {stats.get('avg_latency_ms', 0)}",
        f"trade_latency_p50_ms {stats.get('latency_p50_ms', 0)}",
        f"trade_latency_p90_ms {stats.get('latency_p90_ms', 0)}",
        f"trade_latency_p99_ms {stats.get('latency_p99_ms', 0)}",
        f"clusters_traded {stats.get('clusters_traded', 0)}",
        f"realized_pnl_sol {stats.get('realized_pnl_sol', 0)}",
        f"realized_pnl_sol_24h {stats.get('realized_pnl_sol_24h', 0)}",
        f"realized_pnl_positive_sol {stats.get('realized_pnl_positive_sol', 0)}",
        f"realized_pnl_negative_sol {stats.get('realized_pnl_negative_sol', 0)}",
        f"pnl_wins {stats.get('pnl_wins', 0)}",
        f"pnl_losses {stats.get('pnl_losses', 0)}",
        f"safety_blocks {stats.get('safety_blocks', 0)}",
        f"safety_warnings {stats.get('safety_warnings', 0)}",
        f"exits_executed {stats.get('exits_executed', 0)}",
        f"snipes_attempted {stats.get('snipes_attempted', 0)}",
        f"snipes_successful {stats.get('snipes_successful', 0)}",
        f"snipes_latency_ms {stats.get('snipes_latency_ms', 0)}",
        f"kol_snipes_attempted {stats.get('kol_snipes_attempted', 0)}",
        f"kol_snipes_successful {stats.get('kol_snipes_successful', 0)}",
        f"kol_snipes_latency_ms {stats.get('kol_snipes_latency_ms', 0)}",
        f"open_positions_count {stats.get('open_positions_count', 0)}",
        f"open_positions_sol_total {stats.get('open_positions_sol_total', 0)}",
        f"priority_fee_microlamports_current {stats.get('fee_state', {}).get('priority_fee', 0)}",
    ]
    # Fee/congestion label-like export (simple gauge per level)
    congestion = stats.get("fee_state", {}).get("congestion", "unknown")
    lines.append(f'congestion_level{{level="{congestion}"}} 1')

    # Per-path metrics
    path_sent = stats.get("path_sent", {})
    for p, v in path_sent.items():
        lines.append(f'trade_sent_total{{path="{p}"}} {v}')
    path_failed = stats.get("path_failed", {})
    for p, reasons in path_failed.items():
        for r, v in reasons.items():
            lines.append(f'trade_failed_total{{path="{p}",reason="{r}"}} {v}')
    path_lat_sum = stats.get("path_latency_sum", {})
    path_lat_cnt = stats.get("path_latency_count", {})
    for p, s in path_lat_sum.items():
        c = path_lat_cnt.get(p, 1)
        avg = s / c if c else 0
        lines.append(f'trade_latency_avg_ms{{path="{p}"}} {avg}')
    # Cluster metrics
    clus_det = stats.get("cluster_detected", {})
    for key, v in clus_det.items():
        ctype, bucket = key.split("|", 1)
        lines.append(f'cluster_detected_total{{type="{ctype}",score_bucket="{bucket}"}} {v}')
    clus_auto = stats.get("cluster_autotrade", {})
    for key, v in clus_auto.items():
        res, reason = key.split("|", 1)
        lines.append(f'cluster_autotrade_total{{result="{res}",reason="{reason}"}} {v}')
    clus_score_last = stats.get("cluster_score_last", {})
    for t, v in clus_score_last.items():
        lines.append(f'cluster_score_last{{type="{t}"}} {v}')
    clus_liq_usd_last = stats.get("cluster_liquidity_usd_last", {})
    for t, v in clus_liq_usd_last.items():
        lines.append(f'cluster_liquidity_usd_last{{type="{t}"}} {v}')
    clus_liq_sol_last = stats.get("cluster_liquidity_sol_last", {})
    for t, v in clus_liq_sol_last.items():
        lines.append(f'cluster_liquidity_sol_last{{type="{t}"}} {v}')
    clus_age_last = stats.get("cluster_pool_age_minutes_last", {})
    for t, v in clus_age_last.items():
        lines.append(f'cluster_pool_age_minutes_last{{type="{t}"}} {v}')
    clus_liq_delta_last = stats.get("cluster_liq_delta_5m_usd_last", {})
    for t, v in clus_liq_delta_last.items():
        lines.append(f'cluster_liq_delta_5m_usd_last{{type="{t}"}} {v}')
    clus_liq_delta30_last = stats.get("cluster_liq_delta_30m_usd_last", {})
    for t, v in clus_liq_delta30_last.items():
        lines.append(f'cluster_liq_delta_30m_usd_last{{type="{t}"}} {v}')
    clus_holder_growth_last = stats.get("cluster_holder_growth_24h_last", {})
    for t, v in clus_holder_growth_last.items():
        lines.append(f'cluster_holder_growth_24h_last{{type="{t}"}} {v}')
    clus_unique_growth_last = stats.get("cluster_unique_wallets_24h_delta_last", {})
    for t, v in clus_unique_growth_last.items():
        lines.append(f'cluster_unique_wallets_24h_delta_last{{type="{t}"}} {v}')

    return Response("\n".join(lines) + "\n", mimetype="text/plain")

# Smart money discovery
@app.route('/api/smart-money', methods=['GET'])
def get_smart_money():
    try:
        limit = request.args.get('limit', 50, type=int)
        
        # Get top wallets by win rate
        wallets = db.get_top_wallets(limit)
        
        return jsonify({
            'wallets': wallets,
            'count': len(wallets)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Telegram alerts
@app.route('/api/telegram/alert/cluster', methods=['POST'])
def send_cluster_alert():
    try:
        data = request.json
        chat_id = data.get('chat_id')
        cluster = data.get('cluster')
        
        if not chat_id or not cluster:
            return jsonify({'error': 'Missing chat_id or cluster data'}), 400
        
        # Get token data
        token_data = dexscreener.get_token_data('solana', cluster['token_address'])
        
        # Send alert
        success = telegram_bot.send_cluster_alert(chat_id, cluster, token_data)
        
        return jsonify({
            'success': success,
            'message': 'Alert sent' if success else 'Failed to send alert'
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/telegram/alert/graduation', methods=['POST'])
def send_graduation_alert():
    try:
        data = request.json
        chat_id = data.get('chat_id')
        token_address = data.get('token_address')
        
        if not chat_id or not token_address:
            return jsonify({'error': 'Missing chat_id or token_address'}), 400
        
        # Check graduation
        graduation = dexscreener.check_graduation_status(token_address)
        
        if not graduation.get('graduated'):
            return jsonify({'error': 'Token has not graduated'}), 400
        
        # Get token data
        token_data = dexscreener.get_token_data('solana', token_address)
        
        # Send alert
        success = telegram_bot.send_graduation_alert(chat_id, token_address, graduation, token_data)
        
        return jsonify({
            'success': success,
            'graduation': graduation
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# User preferences
@app.route('/api/user/preferences', methods=['GET', 'POST'])
def user_preferences():
    try:
        if request.method == 'POST':
            data = request.json
            chat_id = data.get('telegram_chat_id')
            tracked_wallets = data.get('tracked_wallets', [])
            alert_settings = data.get('alert_settings', {})
            
            if not chat_id:
                return jsonify({'error': 'Missing telegram_chat_id'}), 400
            
            db.upsert_user_preferences(chat_id, tracked_wallets, alert_settings)
            
            return jsonify({'success': True, 'message': 'Preferences saved'})
        
        else:  # GET
            chat_id = request.args.get('telegram_chat_id')
            
            if not chat_id:
                return jsonify({'error': 'Missing telegram_chat_id'}), 400
            
            prefs = db.get_user_preferences(chat_id)
            
            return jsonify(prefs or {'message': 'No preferences found'})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Monitoring status
@app.route('/api/monitoring/status', methods=['GET'])
def monitoring_status():
    try:
        # Get recent activity
        recent_txs = db.get_recent_transactions(1)
        active_clusters = db.get_active_clusters()
        
        return jsonify({
            'status': 'active',
            'recent_transactions': len(recent_txs),
            'active_clusters': len(active_clusters),
            'database': {
                'path': db.DB_PATH,
                'exists': os.path.exists(db.DB_PATH)
            },
            'services': {
                'dexscreener': True,
                'clustering': True,
                'telegram': telegram_bot.enabled
            }
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Pump.fun Intelligence Platform - INTEGRATED VERSION")
    print("=" * 60)
    print(f"✅ Database: {db.DB_PATH}")
    print(f"✅ DexScreener API: Enabled")
    print(f"✅ Clustering Algorithm: Enabled")
    print(f"✅ Telegram Bot: {'Enabled' if telegram_bot.enabled else 'Disabled (set TELEGRAM_BOT_TOKEN)'}")
    print("=" * 60)
    print("🎯 Ready to detect smart money and make profits!")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=False)

