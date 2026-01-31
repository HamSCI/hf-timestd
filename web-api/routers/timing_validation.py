"""
Timing Validation API Router.

Living Documentation endpoint for validating HF fusion timing against GPS ground truth.
Follows the established pattern: /api/living-docs/evidence/{source}/{filter}

This router provides:
- Real-time validation statistics
- Historical discrepancy data for charting
- Per-minute validation details
"""

import logging
from typing import Optional, List
from datetime import datetime, timezone
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/timing-validation", tags=["timing-validation"])


class ValidationPointResponse(BaseModel):
    """A single validation comparison point."""
    timestamp_utc: str
    minute_boundary: int
    fusion_d_clock_ms: float
    fusion_uncertainty_ms: float
    fusion_n_broadcasts: int
    fusion_quality_grade: str
    gps_d_clock_ms: Optional[float]
    gps_uncertainty_ms: float
    discrepancy_ms: Optional[float]
    within_uncertainty: Optional[bool]
    n_timing_snapshots: int
    timing_authority: str


class ValidationStatisticsResponse(BaseModel):
    """Aggregate validation statistics."""
    start_time: str
    end_time: str
    n_points: int
    mean_discrepancy_ms: float
    std_discrepancy_ms: float
    max_discrepancy_ms: float
    min_discrepancy_ms: float
    within_uncertainty_pct: float
    within_1ms_pct: float
    within_5ms_pct: float
    grade_a_pct: float
    grade_b_pct: float
    grade_c_pct: float
    grade_d_pct: float
    timing_authority: str
    gps_accuracy_ms: float


class ValidationDashboardResponse(BaseModel):
    """Complete dashboard data."""
    statistics: Optional[ValidationStatisticsResponse]
    recent_points: List[ValidationPointResponse]
    last_updated: str
    status: str  # "ok", "no_data", "error"
    message: Optional[str] = None


class TimingSnapshotResponse(BaseModel):
    """Timing snapshot from JSON sidecar."""
    gps_time_ns: int
    rtp_timesnap: int
    local_receipt_time: float
    unix_time: float
    utc_time: str


class MinuteDetailResponse(BaseModel):
    """Detailed validation data for a specific minute."""
    minute_boundary: int
    minute_utc: str
    validation: Optional[ValidationPointResponse]
    timing_snapshots: List[TimingSnapshotResponse]
    fusion_available: bool
    snapshots_available: bool


def _get_service():
    """Lazy import to avoid circular dependencies."""
    try:
        from hf_timestd.core.timing_validation_service import get_validation_service
        return get_validation_service()
    except ImportError as e:
        logger.error(f"Failed to import timing_validation_service: {e}")
        return None


@router.get("/dashboard", response_model=ValidationDashboardResponse)
async def get_validation_dashboard(
    hours: int = Query(1, ge=1, le=24, description="Hours of history to include"),
    points: int = Query(60, ge=10, le=1440, description="Number of recent points to return")
):
    """
    Get complete validation dashboard data.
    
    Returns statistics and recent validation points for display.
    """
    service = _get_service()
    if not service:
        return ValidationDashboardResponse(
            statistics=None,
            recent_points=[],
            last_updated=datetime.now(timezone.utc).isoformat(),
            status="error",
            message="Validation service not available"
        )
    
    try:
        # Run validation scan
        service.run_validation_scan(hours=hours)
        
        # Get statistics
        stats = service.get_statistics(last_n_minutes=hours * 60)
        
        # Get recent points
        recent = service.get_recent_points(n=points)
        
        if not recent:
            return ValidationDashboardResponse(
                statistics=None,
                recent_points=[],
                last_updated=datetime.now(timezone.utc).isoformat(),
                status="no_data",
                message="No validation data available. Ensure fusion service is running and producing output."
            )
        
        # Convert to response models
        stats_response = None
        if stats:
            stats_response = ValidationStatisticsResponse(
                start_time=stats.start_time,
                end_time=stats.end_time,
                n_points=stats.n_points,
                mean_discrepancy_ms=stats.mean_discrepancy_ms,
                std_discrepancy_ms=stats.std_discrepancy_ms,
                max_discrepancy_ms=stats.max_discrepancy_ms,
                min_discrepancy_ms=stats.min_discrepancy_ms,
                within_uncertainty_pct=stats.within_uncertainty_pct,
                within_1ms_pct=stats.within_1ms_pct,
                within_5ms_pct=stats.within_5ms_pct,
                grade_a_pct=stats.grade_a_pct,
                grade_b_pct=stats.grade_b_pct,
                grade_c_pct=stats.grade_c_pct,
                grade_d_pct=stats.grade_d_pct,
                timing_authority=stats.timing_authority,
                gps_accuracy_ms=stats.gps_accuracy_ms
            )
        
        points_response = [
            ValidationPointResponse(
                timestamp_utc=p.timestamp_utc,
                minute_boundary=p.minute_boundary,
                fusion_d_clock_ms=p.fusion_d_clock_ms,
                fusion_uncertainty_ms=p.fusion_uncertainty_ms,
                fusion_n_broadcasts=p.fusion_n_broadcasts,
                fusion_quality_grade=p.fusion_quality_grade,
                gps_d_clock_ms=p.gps_d_clock_ms,
                gps_uncertainty_ms=p.gps_uncertainty_ms,
                discrepancy_ms=p.discrepancy_ms,
                within_uncertainty=p.within_uncertainty,
                n_timing_snapshots=p.n_timing_snapshots,
                timing_authority=p.timing_authority
            )
            for p in recent
        ]
        
        return ValidationDashboardResponse(
            statistics=stats_response,
            recent_points=points_response,
            last_updated=datetime.now(timezone.utc).isoformat(),
            status="ok"
        )
        
    except Exception as e:
        logger.error(f"Error in validation dashboard: {e}")
        return ValidationDashboardResponse(
            statistics=None,
            recent_points=[],
            last_updated=datetime.now(timezone.utc).isoformat(),
            status="error",
            message=str(e)
        )


@router.get("/statistics", response_model=Optional[ValidationStatisticsResponse])
async def get_validation_statistics(
    hours: int = Query(1, ge=1, le=24, description="Hours of history to include")
):
    """Get validation statistics for the specified time range."""
    service = _get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Validation service not available")
    
    service.run_validation_scan(hours=hours)
    stats = service.get_statistics(last_n_minutes=hours * 60)
    
    if not stats:
        return None
    
    return ValidationStatisticsResponse(
        start_time=stats.start_time,
        end_time=stats.end_time,
        n_points=stats.n_points,
        mean_discrepancy_ms=stats.mean_discrepancy_ms,
        std_discrepancy_ms=stats.std_discrepancy_ms,
        max_discrepancy_ms=stats.max_discrepancy_ms,
        min_discrepancy_ms=stats.min_discrepancy_ms,
        within_uncertainty_pct=stats.within_uncertainty_pct,
        within_1ms_pct=stats.within_1ms_pct,
        within_5ms_pct=stats.within_5ms_pct,
        grade_a_pct=stats.grade_a_pct,
        grade_b_pct=stats.grade_b_pct,
        grade_c_pct=stats.grade_c_pct,
        grade_d_pct=stats.grade_d_pct,
        timing_authority=stats.timing_authority,
        gps_accuracy_ms=stats.gps_accuracy_ms
    )


@router.get("/minute/{minute_boundary}", response_model=MinuteDetailResponse)
async def get_minute_detail(minute_boundary: int):
    """
    Get detailed validation data for a specific minute.
    
    Includes all timing snapshots and fusion result.
    """
    service = _get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Validation service not available")
    
    # Get timing snapshots
    snapshots = service.get_timing_snapshots_for_minute(minute_boundary)
    
    # Get validation point
    validation = service.validate_minute(minute_boundary)
    
    # Check fusion availability
    fusion = service.load_fusion_result(minute_boundary)
    
    # Convert snapshots
    snapshot_responses = [
        TimingSnapshotResponse(
            gps_time_ns=s.gps_time_ns,
            rtp_timesnap=s.rtp_timesnap,
            local_receipt_time=s.local_receipt_time,
            unix_time=s.unix_time,
            utc_time=datetime.utcfromtimestamp(s.unix_time).isoformat() + "Z"
        )
        for s in snapshots
    ]
    
    # Convert validation point
    validation_response = None
    if validation:
        validation_response = ValidationPointResponse(
            timestamp_utc=validation.timestamp_utc,
            minute_boundary=validation.minute_boundary,
            fusion_d_clock_ms=validation.fusion_d_clock_ms,
            fusion_uncertainty_ms=validation.fusion_uncertainty_ms,
            fusion_n_broadcasts=validation.fusion_n_broadcasts,
            fusion_quality_grade=validation.fusion_quality_grade,
            gps_d_clock_ms=validation.gps_d_clock_ms,
            gps_uncertainty_ms=validation.gps_uncertainty_ms,
            discrepancy_ms=validation.discrepancy_ms,
            within_uncertainty=validation.within_uncertainty,
            n_timing_snapshots=validation.n_timing_snapshots,
            timing_authority=validation.timing_authority
        )
    
    return MinuteDetailResponse(
        minute_boundary=minute_boundary,
        minute_utc=datetime.utcfromtimestamp(minute_boundary).isoformat() + "Z",
        validation=validation_response,
        timing_snapshots=snapshot_responses,
        fusion_available=fusion is not None,
        snapshots_available=len(snapshots) > 0
    )


@router.get("/recent", response_model=List[ValidationPointResponse])
async def get_recent_validations(
    n: int = Query(60, ge=1, le=1440, description="Number of recent points")
):
    """Get the most recent validation points."""
    service = _get_service()
    if not service:
        raise HTTPException(status_code=503, detail="Validation service not available")
    
    recent = service.get_recent_points(n=n)
    
    return [
        ValidationPointResponse(
            timestamp_utc=p.timestamp_utc,
            minute_boundary=p.minute_boundary,
            fusion_d_clock_ms=p.fusion_d_clock_ms,
            fusion_uncertainty_ms=p.fusion_uncertainty_ms,
            fusion_n_broadcasts=p.fusion_n_broadcasts,
            fusion_quality_grade=p.fusion_quality_grade,
            gps_d_clock_ms=p.gps_d_clock_ms,
            gps_uncertainty_ms=p.gps_uncertainty_ms,
            discrepancy_ms=p.discrepancy_ms,
            within_uncertainty=p.within_uncertainty,
            n_timing_snapshots=p.n_timing_snapshots,
            timing_authority=p.timing_authority
        )
        for p in recent
    ]
