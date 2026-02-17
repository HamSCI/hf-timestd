"""
Decoder configuration and A/B comparison service.

Manages parallel decoder execution and writes comparison metrics to HDF5.
Provides configuration toggles for selecting primary decoder variant.

Author: AI Assistant
Date: 2026-02-16
"""

import os
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List, Any
from enum import Enum
from pathlib import Path
import numpy as np

logger = logging.getLogger(__name__)


class DecoderVariant(Enum):
    """Available decoder variants."""
    MATCHED_FILTER = "matched_filter"
    PLL = "pll"
    BOTH = "both"  # Run both for A/B comparison


@dataclass
class DecoderConfig:
    """Configuration for decoder selection and A/B testing."""
    
    # Primary decoder selection
    primary_decoder: DecoderVariant = DecoderVariant.MATCHED_FILTER
    
    # A/B testing settings
    enable_ab_comparison: bool = True  # Run both and compare
    ab_test_duration_days: int = 7     # Duration before auto-selection
    
    # Auto-promotion criteria
    auto_promote_pll: bool = False     # Auto-promote PLL if superior
    pll_superiority_threshold: float = 0.7  # PLL must be >70% better
    
    # Individual decoder settings
    pll_window_ms: float = 40.0        # PLL gating window
    pll_alpha: float = 0.1             # PLL loop gain
    pll_max_missed: int = 5            # PLL coast duration
    
    # Logging
    log_comparison_every_n_minutes: int = 1
    
    # Runtime state (not from environment)
    ab_test_start_time: Optional[datetime] = None
    comparison_metrics: Optional['ComparisonMetrics'] = None
    
    @classmethod
    def from_environment(cls) -> "DecoderConfig":
        """Load configuration from environment variables."""
        config = cls()
        
        # Primary decoder selection
        decoder = os.getenv("TIMESTD_DECODER_VARIANT", "matched_filter").lower()
        if decoder == "pll":
            config.primary_decoder = DecoderVariant.PLL
        elif decoder == "both":
            config.primary_decoder = DecoderVariant.BOTH
        else:
            config.primary_decoder = DecoderVariant.MATCHED_FILTER
        
        # A/B testing
        config.enable_ab_comparison = os.getenv(
            "TIMESTD_ENABLE_AB_COMPARISON", "true"
        ).lower() in ("true", "1", "yes")
        
        config.ab_test_duration_days = int(os.getenv(
            "TIMESTD_AB_TEST_DURATION_DAYS", "7"
        ))
        
        # Auto-promotion
        config.auto_promote_pll = os.getenv(
            "TIMESTD_AUTO_PROMOTE_PLL", "false"
        ).lower() in ("true", "1", "yes")
        
        config.pll_superiority_threshold = float(os.getenv(
            "TIMESTD_PLL_SUPERIORITY_THRESHOLD", "0.7"
        ))
        
        # PLL parameters
        config.pll_window_ms = float(os.getenv(
            "TIMESTD_PLL_WINDOW_MS", "40.0"
        ))
        config.pll_alpha = float(os.getenv(
            "TIMESTD_PLL_ALPHA", "0.1"
        ))
        config.pll_max_missed = int(os.getenv(
            "TIMESTD_PLL_MAX_MISSED", "5"
        ))
        
        return config
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "primary_decoder": self.primary_decoder.value,
            "enable_ab_comparison": self.enable_ab_comparison,
            "ab_test_duration_days": self.ab_test_duration_days,
            "auto_promote_pll": self.auto_promote_pll,
            "pll_superiority_threshold": self.pll_superiority_threshold,
            "pll_window_ms": self.pll_window_ms,
            "pll_alpha": self.pll_alpha,
            "pll_max_missed": self.pll_max_missed,
        }
    
    def get_running_decoders(self) -> List[str]:
        """Return list of currently running decoders."""
        if self.enable_ab_comparison or self.primary_decoder == DecoderVariant.BOTH:
            return ["matched_filter", "pll"]
        return [self.primary_decoder.value]
    
    def can_auto_promote(self) -> bool:
        """Check if auto-promotion criteria are met."""
        if not self.auto_promote_pll:
            return False
        if not self.comparison_metrics:
            return False
        # Check if test duration has elapsed
        if self.ab_test_start_time:
            days_elapsed = (datetime.utcnow() - self.ab_test_start_time).total_seconds() / 86400
            return days_elapsed >= self.ab_test_duration_days
        return False
    
    def update_comparison_metrics(self, tracker: 'DecoderComparisonTracker'):
        """Update comparison metrics from tracker for API exposure."""
        summary = tracker.get_daily_summary()
        if summary.get("status") != "insufficient_data":
            self.comparison_metrics = ComparisonMetrics(
                matched_filter_accuracy=summary.get("mf_mean_error_ms"),
                pll_accuracy=summary.get("pll_mean_error_ms"),
                matched_filter_ticks=summary.get("mf_count", 0),
                pll_ticks=summary.get("pll_count", 0),
                winner=tracker.current_winner
            )
    
    @property
    def superiority_threshold(self) -> float:
        """Alias for pll_superiority_threshold for backward compatibility."""
        return self.pll_superiority_threshold


@dataclass
class ComparisonMetrics:
    """Metrics from a single A/B comparison sample."""
    matched_filter_accuracy: Optional[float] = None
    pll_accuracy: Optional[float] = None
    accuracy_improvement_pct: Optional[float] = None
    matched_filter_ticks: int = 0
    pll_ticks: int = 0
    pll_lock_quality: Optional[float] = None
    winner: DecoderVariant = DecoderVariant.MATCHED_FILTER
    samples_since: Optional[datetime] = None


class DecoderComparisonTracker:
    """
    Tracks A/B comparison metrics between decoder variants.
    
    Accumulates statistics over time and determines which decoder
    variant is superior for accuracy, robustness, and convergence.
    """
    
    def __init__(self, config: DecoderConfig):
        self.config = config
        
        # Running statistics
        self.mf_errors = []  # List of (timestamp, error_ms) tuples
        self.pll_errors = []
        self.mf_n_ticks = []
        self.pll_n_ticks = []
        
        # Daily summaries
        self.daily_summaries = []
        
        # Winner determination
        self.current_winner = DecoderVariant.MATCHED_FILTER
        self.winner_confidence = 0.0
        self.winner_since = None
        
        logger.info(
            f"DecoderComparisonTracker initialized: "
            f"primary={config.primary_decoder.value}, "
            f"ab_enabled={config.enable_ab_comparison}"
        )
    
    def add_comparison(
        self,
        timestamp: float,
        mf_d_clock: Optional[float],
        pll_d_clock: Optional[float],
        mf_n_ticks: int,
        pll_n_ticks: int,
        gps_reference: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Add a comparison point between decoders.
        
        Args:
            timestamp: Unix timestamp
            mf_d_clock: Matched filter D_clock (ms)
            pll_d_clock: PLL D_clock (ms)
            mf_n_ticks: Number of ticks from matched filter
            pll_n_ticks: Number of ticks from PLL
            gps_reference: Optional GPS ground truth D_clock (ms)
            
        Returns:
            Comparison result dict with winner determination
        """
        result = {
            "timestamp": timestamp,
            "mf_d_clock_ms": mf_d_clock,
            "pll_d_clock_ms": pll_d_clock,
            "delta_ms": None,
            "mf_n_ticks": mf_n_ticks,
            "pll_n_ticks": pll_n_ticks,
            "mf_gps_error_ms": None,
            "pll_gps_error_ms": None,
            "winner": "NONE",
            "winner_confidence": 0.0,
        }
        
        # Calculate delta
        if mf_d_clock is not None and pll_d_clock is not None:
            delta = pll_d_clock - mf_d_clock
            result["delta_ms"] = delta
        
        # Compare against GPS if available
        if gps_reference is not None:
            if mf_d_clock is not None:
                mf_error = abs(mf_d_clock - gps_reference)
                result["mf_gps_error_ms"] = mf_error
                self.mf_errors.append((timestamp, mf_error))
            
            if pll_d_clock is not None:
                pll_error = abs(pll_d_clock - gps_reference)
                result["pll_gps_error_ms"] = pll_error
                self.pll_errors.append((timestamp, pll_error))
            
            # Determine winner based on GPS
            if result["mf_gps_error_ms"] is not None and result["pll_gps_error_ms"] is not None:
                if result["pll_gps_error_ms"] < result["mf_gps_error_ms"] * self.config.pll_superiority_threshold:
                    result["winner"] = "PLL"
                    result["winner_confidence"] = 0.8
                elif result["mf_gps_error_ms"] < result["pll_gps_error_ms"] * self.config.pll_superiority_threshold:
                    result["winner"] = "MF"
                    result["winner_confidence"] = 0.8
                else:
                    result["winner"] = "TIE"
                    result["winner_confidence"] = 0.5
        else:
            # No GPS reference - use internal consistency
            # Lower std and higher tick count wins
            if mf_n_ticks > 0 and pll_n_ticks > 0:
                tick_ratio = pll_n_ticks / max(mf_n_ticks, 1)
                
                if tick_ratio > 1.2:  # PLL detected significantly more ticks
                    result["winner"] = "PLL"
                    result["winner_confidence"] = 0.6
                elif tick_ratio < 0.8:  # MF detected more
                    result["winner"] = "MF"
                    result["winner_confidence"] = 0.6
                else:
                    result["winner"] = "TIE"
                    result["winner_confidence"] = 0.5
        
        # Clean old data (keep last 24 hours)
        cutoff = timestamp - 86400
        self.mf_errors = [(t, e) for t, e in self.mf_errors if t > cutoff]
        self.pll_errors = [(t, e) for t, e in self.pll_errors if t > cutoff]
        
        return result
    
    def get_daily_summary(self) -> Dict[str, Any]:
        """Compute daily performance summary."""
        if not self.mf_errors and not self.pll_errors:
            return {"status": "insufficient_data"}
        
        summary = {
            "timestamp": time.time(),
            "mf_count": len(self.mf_errors),
            "pll_count": len(self.pll_errors),
        }
        
        if self.mf_errors:
            mf_vals = [e for _, e in self.mf_errors]
            summary["mf_mean_error_ms"] = np.mean(mf_vals)
            summary["mf_std_error_ms"] = np.std(mf_vals)
            summary["mf_max_error_ms"] = np.max(mf_vals)
        
        if self.pll_errors:
            pll_vals = [e for _, e in self.pll_errors]
            summary["pll_mean_error_ms"] = np.mean(pll_vals)
            summary["pll_std_error_ms"] = np.std(pll_vals)
            summary["pll_max_error_ms"] = np.max(pll_vals)
        
        # Overall winner
        if self.mf_errors and self.pll_errors:
            mf_mean = summary["mf_mean_error_ms"]
            pll_mean = summary["pll_mean_error_ms"]
            
            if pll_mean < mf_mean * 0.8:  # PLL >20% better
                summary["recommendation"] = "PROMOTE_PLL"
            elif mf_mean < pll_mean * 0.8:  # MF >20% better
                summary["recommendation"] = "KEEP_MF"
            else:
                summary["recommendation"] = "EQUIVALENT"
        
        return summary
    
    def should_auto_promote(self) -> bool:
        """Check if PLL should be auto-promoted based on accumulated data."""
        if not self.config.auto_promote_pll:
            return False
        
        if not self.mf_errors or not self.pll_errors:
            return False
        
        # Check if we have enough data (ab_test_duration_days)
        if len(self.daily_summaries) < self.config.ab_test_duration_days:
            return False
        
        # Check if PLL has been consistently superior
        pll_superior_days = sum(
            1 for s in self.daily_summaries
            if s.get("recommendation") == "PROMOTE_PLL"
        )
        
        return pll_superior_days >= self.config.ab_test_duration_days * 0.7  # 70% of days


# Singleton instance for shared state between API and metrology
_decoder_config_instance: Optional[DecoderConfig] = None

def get_decoder_config() -> DecoderConfig:
    """Get decoder configuration from environment (singleton)."""
    global _decoder_config_instance
    if _decoder_config_instance is None:
        _decoder_config_instance = DecoderConfig.from_environment()
    return _decoder_config_instance


def reset_decoder_config() -> None:
    """Reset the singleton instance (for testing)."""
    global _decoder_config_instance
    _decoder_config_instance = None


def log_decoder_selection(config: DecoderConfig, tracker: DecoderComparisonTracker):
    """Log current decoder selection and comparison status."""
    summary = tracker.get_daily_summary()
    
    logger.info("=" * 60)
    logger.info("DECODER A/B COMPARISON STATUS")
    logger.info("=" * 60)
    logger.info(f"Primary decoder: {config.primary_decoder.value}")
    logger.info(f"A/B comparison enabled: {config.enable_ab_comparison}")
    
    if summary.get("status") != "insufficient_data":
        logger.info(f"MF samples: {summary.get('mf_count', 0)}")
        logger.info(f"PLL samples: {summary.get('pll_count', 0)}")
        
        if "mf_mean_error_ms" in summary:
            logger.info(f"MF mean error: {summary['mf_mean_error_ms']:.3f} ms")
        if "pll_mean_error_ms" in summary:
            logger.info(f"PLL mean error: {summary['pll_mean_error_ms']:.3f} ms")
        
        logger.info(f"Recommendation: {summary.get('recommendation', 'UNKNOWN')}")
        
        if tracker.should_auto_promote():
            logger.warning("AUTO-PROMOTION TRIGGER: PLL should be promoted to primary")
    else:
        logger.info("Insufficient data for comparison")
    
    logger.info("=" * 60)
