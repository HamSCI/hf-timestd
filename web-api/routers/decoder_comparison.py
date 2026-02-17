"""
Decoder Comparison API Router

Provides endpoints for A/B comparison between Matched Filter and PLL decoders.

Endpoints:
- /decoder-comparison/status: Current decoder status and winner
- /decoder-comparison/metrics: Comparison metrics over time
- /decoder-comparison/recommendation: Auto-promotion recommendation
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from hf_timestd.core import get_decoder_config, DecoderVariant, ComparisonMetrics
from hf_timestd.io.hdf5_reader import DataProductReader
from config import config

router = APIRouter(prefix="/decoder-comparison", tags=["decoder-comparison"])
logger = logging.getLogger(__name__)


@router.get("/status")
async def get_decoder_status():
    """
    Get current decoder A/B testing status.
    
    Returns:
    - Which decoders are running (matched_filter, pll, both)
    - Current "winner" based on accuracy
    - Days remaining in A/B test period
    - Auto-promotion settings
    """
    try:
        cfg = get_decoder_config()
        
        # Calculate test progress
        if cfg.ab_test_start_time:
            days_elapsed = (datetime.utcnow() - cfg.ab_test_start_time).total_seconds() / 86400
            days_remaining = max(0, cfg.ab_test_duration_days - days_elapsed)
            percent_complete = min(100, (days_elapsed / cfg.ab_test_duration_days) * 100)
        else:
            days_elapsed = 0
            days_remaining = cfg.ab_test_duration_days
            percent_complete = 0
        
        # Get latest metrics if available
        latest_metrics = None
        winner = None
        if cfg.comparison_metrics:
            latest_metrics = {
                'matched_filter_accuracy': cfg.comparison_metrics.matched_filter_accuracy,
                'pll_accuracy': cfg.comparison_metrics.pll_accuracy,
                'accuracy_improvement_pct': cfg.comparison_metrics.accuracy_improvement_pct,
                'matched_filter_ticks': cfg.comparison_metrics.matched_filter_ticks,
                'pll_ticks': cfg.comparison_metrics.pll_ticks,
                'pll_lock_quality': cfg.comparison_metrics.pll_lock_quality,
                'samples_since': cfg.comparison_metrics.samples_since.isoformat() if cfg.comparison_metrics.samples_since else None,
            }
            winner = cfg.comparison_metrics.winner.value
        
        return {
            'primary_decoder': cfg.primary_decoder.value,
            'running_decoders': cfg.get_running_decoders(),
            'ab_testing_enabled': cfg.enable_ab_comparison,
            'ab_test_progress': {
                'days_elapsed': round(days_elapsed, 1),
                'days_remaining': round(days_remaining, 1),
                'percent_complete': round(percent_complete, 1),
                'duration_days': cfg.ab_test_duration_days,
            },
            'auto_promote_pll': cfg.auto_promote_pll,
            'superiority_threshold': cfg.superiority_threshold,
            'latest_comparison': latest_metrics,
            'current_winner': winner,
            'can_auto_promote': cfg.can_auto_promote(),
            'recommendation': cfg.get_promotion_recommendation() if cfg.can_auto_promote() else None,
        }
    
    except Exception as e:
        logger.error(f"Error getting decoder status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/metrics")
async def get_comparison_metrics(
    hours: int = Query(24, ge=1, le=168, description="Hours of history to retrieve"),
    broadcast_id: Optional[str] = Query(None, description="Filter by broadcast ID (e.g., WWV_10000)")
):
    """
    Get A/B comparison metrics over time.
    
    Returns per-broadcast or aggregated metrics comparing:
    - Matched Filter vs PLL accuracy (std dev of timing error)
    - Tick detection counts
    - PLL lock quality
    - GPS error comparison
    """
    try:
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=hours)
        
        metrics_data = []
        
        # Read from decoder_comparison data product
        phase2_dir = config.data_root / 'phase2'
        
        for channel_dir in phase2_dir.iterdir():
            if not channel_dir.is_dir() or channel_dir.name in ['fusion', 'science']:
                continue
            
            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='decoder_comparison',
                    channel=channel_dir.name
                )
                
                measurements = reader.read_time_range(
                    start=start_time.isoformat() + 'Z',
                    end=end_time.isoformat() + 'Z'
                )
                
                for m in measurements:
                    # Filter by broadcast_id if specified
                    if broadcast_id:
                        station = m.get('station', '')
                        freq_mhz = m.get('frequency_mhz', 0)
                        freq_khz = int(round(freq_mhz * 1000))
                        if f"{station}_{freq_khz}" != broadcast_id:
                            continue
                    
                    metrics_data.append({
                        'timestamp': m.get('timestamp_utc'),
                        'broadcast_id': m.get('broadcast_id'),
                        'station': m.get('station'),
                        'frequency_mhz': m.get('frequency_mhz'),
                        'matched_filter_accuracy': m.get('matched_filter_accuracy_ms'),
                        'pll_accuracy': m.get('pll_accuracy_ms'),
                        'accuracy_improvement_pct': m.get('accuracy_improvement_pct'),
                        'matched_filter_ticks': m.get('matched_filter_ticks'),
                        'pll_ticks': m.get('pll_ticks'),
                        'pll_lock_quality': m.get('pll_lock_quality'),
                        'winner': m.get('winner'),
                        'gps_error_matched_filter': m.get('gps_error_matched_filter_ms'),
                        'gps_error_pll': m.get('gps_error_pll_ms'),
                    })
                
            except Exception as e:
                logger.debug(f"Could not read decoder_comparison from {channel_dir.name}: {e}")
                continue
        
        # Aggregate by timestamp
        from collections import defaultdict
        by_timestamp = defaultdict(lambda: {
            'matched_filter_accuracies': [],
            'pll_accuracies': [],
            'matched_filter_ticks': 0,
            'pll_ticks': 0,
            'winners': [],
        })
        
        for m in metrics_data:
            ts = m['timestamp']
            by_timestamp[ts]['matched_filter_accuracies'].append(m['matched_filter_accuracy'])
            by_timestamp[ts]['pll_accuracies'].append(m['pll_accuracy'])
            by_timestamp[ts]['matched_filter_ticks'] += m['matched_filter_ticks'] or 0
            by_timestamp[ts]['pll_ticks'] += m['pll_ticks'] or 0
            if m['winner']:
                by_timestamp[ts]['winners'].append(m['winner'])
        
        # Calculate aggregates
        aggregated = []
        for ts, data in sorted(by_timestamp.items()):
            mf_accs = [a for a in data['matched_filter_accuracies'] if a is not None]
            pll_accs = [a for a in data['pll_accuracies'] if a is not None]
            
            # Count winners
            winner_counts = {}
            for w in data['winners']:
                winner_counts[w] = winner_counts.get(w, 0) + 1
            dominant_winner = max(winner_counts, key=winner_counts.get) if winner_counts else None
            
            aggregated.append({
                'timestamp': ts,
                'matched_filter_accuracy_ms': sum(mf_accs) / len(mf_accs) if mf_accs else None,
                'pll_accuracy_ms': sum(pll_accs) / len(pll_accs) if pll_accs else None,
                'matched_filter_total_ticks': data['matched_filter_ticks'],
                'pll_total_ticks': data['pll_ticks'],
                'dominant_winner': dominant_winner,
            })
        
        return {
            'time_range': {
                'start': start_time.isoformat() + 'Z',
                'end': end_time.isoformat() + 'Z',
            },
            'n_measurements': len(metrics_data),
            'raw_metrics': metrics_data,
            'aggregated': aggregated,
        }
    
    except Exception as e:
        logger.error(f"Error getting comparison metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/recommendation")
async def get_promotion_recommendation():
    """
    Get auto-promotion recommendation.
    
    Returns detailed analysis of whether PLL should be promoted
to primary decoder based on collected metrics.
    """
    try:
        cfg = get_decoder_config()
        
        if not cfg.can_auto_promote():
            return {
                'can_promote': False,
                'reason': 'A/B test period not complete or insufficient data',
                'test_progress_pct': 0,
            }
        
        recommendation = cfg.get_promotion_recommendation()
        
        if not recommendation:
            return {
                'can_promote': False,
                'reason': 'No recommendation available',
            }
        
        return {
            'can_promote': True,
            'recommendation': recommendation.value,
            'test_duration_days': cfg.ab_test_duration_days,
            'days_elapsed': (datetime.utcnow() - cfg.ab_test_start_time).total_seconds() / 86400 if cfg.ab_test_start_time else 0,
            'metrics_summary': {
                'matched_filter_accuracy': cfg.comparison_metrics.matched_filter_accuracy if cfg.comparison_metrics else None,
                'pll_accuracy': cfg.comparison_metrics.pll_accuracy if cfg.comparison_metrics else None,
                'accuracy_improvement_pct': cfg.comparison_metrics.accuracy_improvement_pct if cfg.comparison_metrics else None,
                'superiority_threshold_pct': cfg.superiority_threshold * 100,
            },
            'action_required': recommendation == DecoderVariant.PLL,
            'message': 'PLL decoder should be promoted to primary' if recommendation == DecoderVariant.PLL else 'Keep matched filter as primary decoder',
        }
    
    except Exception as e:
        logger.error(f"Error getting promotion recommendation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/broadcasts/{broadcast_id}")
async def get_broadcast_comparison(
    broadcast_id: str,
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Get A/B comparison metrics for a specific broadcast.
    
    Args:
        broadcast_id: Broadcast ID (e.g., WWV_10000, CHU_7850)
    """
    try:
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=hours)
        
        # Parse broadcast_id
        parts = broadcast_id.rsplit('_', 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="Invalid broadcast_id format. Use: STATION_FREQUENCY (e.g., WWV_10000)")
        
        target_station, target_freq = parts
        target_freq_khz = int(target_freq)
        
        metrics = []
        
        # Read from decoder_comparison
        phase2_dir = config.data_root / 'phase2'
        
        for channel_dir in phase2_dir.iterdir():
            if not channel_dir.is_dir() or channel_dir.name in ['fusion', 'science']:
                continue
            
            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='decoder_comparison',
                    channel=channel_dir.name
                )
                
                measurements = reader.read_time_range(
                    start=start_time.isoformat() + 'Z',
                    end=end_time.isoformat() + 'Z'
                )
                
                for m in measurements:
                    station = m.get('station', '')
                    freq_mhz = m.get('frequency_mhz', 0)
                    freq_khz = int(round(freq_mhz * 1000))
                    
                    if station == target_station and freq_khz == target_freq_khz:
                        metrics.append({
                            'timestamp': m.get('timestamp_utc'),
                            'matched_filter_mean_offset': m.get('matched_filter_mean_timing_offset_ms'),
                            'pll_mean_offset': m.get('pll_mean_timing_offset_ms'),
                            'matched_filter_std': m.get('matched_filter_std_timing_ms'),
                            'pll_std': m.get('pll_std_timing_ms'),
                            'winner': m.get('winner'),
                            'pll_state': m.get('pll_state'),
                        })
                
            except Exception as e:
                logger.debug(f"Could not read from {channel_dir.name}: {e}")
                continue
        
        # Calculate statistics
        if metrics:
            mf_stds = [m['matched_filter_std'] for m in metrics if m['matched_filter_std'] is not None]
            pll_stds = [m['pll_std'] for m in metrics if m['pll_std'] is not None]
            
            avg_mf_accuracy = sum(mf_stds) / len(mf_stds) if mf_stds else None
            avg_pll_accuracy = sum(pll_stds) / len(pll_stds) if pll_stds else None
            
            # Count winners
            winner_counts = {}
            for m in metrics:
                if m['winner']:
                    winner_counts[m['winner']] = winner_counts.get(m['winner'], 0) + 1
        else:
            avg_mf_accuracy = None
            avg_pll_accuracy = None
            winner_counts = {}
        
        return {
            'broadcast_id': broadcast_id,
            'time_range': {
                'start': start_time.isoformat() + 'Z',
                'end': end_time.isoformat() + 'Z',
            },
            'n_measurements': len(metrics),
            'average_matched_filter_accuracy_ms': avg_mf_accuracy,
            'average_pll_accuracy_ms': avg_pll_accuracy,
            'winner_counts': winner_counts,
            'dominant_winner': max(winner_counts, key=winner_counts.get) if winner_counts else None,
            'timeseries': sorted(metrics, key=lambda x: x['timestamp']),
        }
    
    except Exception as e:
        logger.error(f"Error getting broadcast comparison: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/promote/{decoder}")
async def promote_decoder(decoder: str):
    """
    Manually promote a decoder to primary.
    
    Args:
        decoder: Decoder to promote ('matched_filter' or 'pll')
    
    This updates the environment configuration to use the specified
decoder as primary. Requires service restart to take effect.
    """
    try:
        if decoder not in ['matched_filter', 'pll']:
            raise HTTPException(status_code=400, detail="Decoder must be 'matched_filter' or 'pll'")
        
        # Update environment file
        env_file = Path('/etc/hf-timestd/environment')
        if not env_file.exists():
            raise HTTPException(status_code=500, detail="Environment file not found")
        
        # Read current content
        content = env_file.read_text()
        
        # Update TIMESTD_DECODER_VARIANT
        import re
        if re.search(r'^TIMESTD_DECODER_VARIANT=', content, re.MULTILINE):
            content = re.sub(
                r'^TIMESTD_DECODER_VARIANT=.*$',
                f'TIMESTD_DECODER_VARIANT={decoder}',
                content,
                flags=re.MULTILINE
            )
        else:
            content += f"\nTIMESTD_DECODER_VARIANT={decoder}\n"
        
        # Write back
        env_file.write_text(content)
        
        return {
            'success': True,
            'message': f"Promoted {decoder} to primary decoder",
            'note': 'Restart services to apply: sudo systemctl restart timestd-metrology',
            'decoder': decoder,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error promoting decoder: {e}")
        raise HTTPException(status_code=500, detail=str(e))
