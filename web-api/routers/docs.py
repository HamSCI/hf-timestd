"""
Living Documentation Router.

Serves Markdown documentation with embedded live data widget directives
and contextual log streaming. This enables documentation-driven development
where the explanation of what the code should do is directly connected to
what it actually does.

Directive syntax (HTML comments, invisible in GitHub):
    <!-- LIVE: widget-type -->
    <!-- LOGS: tag-filter | filter: "search string" -->
"""

import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/living-docs", tags=["documentation"])

# Path to documentation directory (relative to web-api parent)
# Use resolve() to get absolute path from the actual file location
DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"

# Available documents
AVAILABLE_DOCS = {
    "METROLOGY": "METROLOGY.md",
    "PHYSICS": "PHYSICS.md",
    "IONOSPHERIC_RESOLUTION": "IONOSPHERIC_RESOLUTION.md",
    "DUAL_CHRONY_FEED_ARCHITECTURE": "DUAL_CHRONY_FEED_ARCHITECTURE.md",
    "BOOTSTRAP_METHODOLOGY": "BOOTSTRAP_METHODOLOGY.md",
    "IONOSPHERIC_REANALYSIS": "IONOSPHERIC_REANALYSIS.md",
    "TECHNICAL_REFERENCE": "../TECHNICAL_REFERENCE.md",
}


class Directive(BaseModel):
    """A parsed directive from the Markdown."""
    type: str  # 'LIVE' or 'LOGS'
    value: str  # widget type or log tag
    options: Dict[str, str] = {}  # additional options like filter
    line_number: int  # position in document
    section: Optional[str] = None  # nearest heading


class DocumentResponse(BaseModel):
    """Response containing document content and metadata."""
    name: str
    title: str
    markdown: str
    directives: List[Directive]
    sections: List[Dict[str, Any]]  # {id, title, level, line}


class DocumentListResponse(BaseModel):
    """List of available documents."""
    documents: List[Dict[str, str]]


def parse_directives(markdown: str) -> List[Directive]:
    """
    Parse LIVE and LOGS directives from Markdown content.
    
    Directives are HTML comments:
        <!-- LIVE: widget-type -->
        <!-- LOGS: tag | filter: "search" -->
    """
    directives = []
    
    # Pattern for directives: <!-- TYPE: value | option: "value" -->
    pattern = r'<!--\s*(LIVE|LOGS):\s*([^|>]+?)(?:\s*\|\s*(.+?))?\s*-->'
    
    lines = markdown.split('\n')
    current_section = None
    
    for line_num, line in enumerate(lines, 1):
        # Track current section (heading)
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            current_section = heading_match.group(2).strip()
        
        # Find directives
        for match in re.finditer(pattern, line):
            directive_type = match.group(1)
            value = match.group(2).strip()
            options_str = match.group(3)
            
            options = {}
            if options_str:
                # Parse options like: filter: "search string"
                opt_pattern = r'(\w+):\s*"([^"]*)"'
                for opt_match in re.finditer(opt_pattern, options_str):
                    options[opt_match.group(1)] = opt_match.group(2)
            
            directives.append(Directive(
                type=directive_type,
                value=value,
                options=options,
                line_number=line_num,
                section=current_section
            ))
    
    return directives


def parse_sections(markdown: str) -> List[Dict[str, Any]]:
    """Extract section headings for navigation."""
    sections = []
    lines = markdown.split('\n')
    
    for line_num, line in enumerate(lines, 1):
        match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            # Create URL-friendly ID
            section_id = re.sub(r'[^\w\s-]', '', title.lower())
            section_id = re.sub(r'[\s]+', '-', section_id)
            
            sections.append({
                'id': section_id,
                'title': title,
                'level': level,
                'line': line_num
            })
    
    return sections


def extract_title(markdown: str) -> str:
    """Extract document title from first H1 heading."""
    match = re.search(r'^#\s+(.+)$', markdown, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return "Untitled"


@router.get("/list", response_model=DocumentListResponse)
async def list_documents():
    """List available documentation files."""
    docs = []
    for key, filename in AVAILABLE_DOCS.items():
        filepath = DOCS_DIR / filename
        if filepath.exists():
            content = filepath.read_text()
            title = extract_title(content)
            docs.append({
                'key': key,
                'filename': filename,
                'title': title
            })
    
    return DocumentListResponse(documents=docs)


@router.get("/{doc_name}", response_model=DocumentResponse)
async def get_document(doc_name: str, section: Optional[str] = None):
    """
    Get a documentation file with parsed directives.
    
    Args:
        doc_name: Document key (e.g., 'METROLOGY')
        section: Optional section ID to extract (returns full doc if not specified)
    """
    if doc_name not in AVAILABLE_DOCS:
        raise HTTPException(
            status_code=404,
            detail=f"Document '{doc_name}' not found. Available: {list(AVAILABLE_DOCS.keys())}"
        )
    
    filepath = DOCS_DIR / AVAILABLE_DOCS[doc_name]
    if not filepath.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Document file not found: {filepath}"
        )
    
    markdown = filepath.read_text()
    title = extract_title(markdown)
    directives = parse_directives(markdown)
    sections = parse_sections(markdown)
    
    # If section requested, extract just that section
    if section:
        markdown = extract_section(markdown, section, sections)
        # Re-parse directives for the extracted section
        directives = parse_directives(markdown)
    
    return DocumentResponse(
        name=doc_name,
        title=title,
        markdown=markdown,
        directives=directives,
        sections=sections
    )


def extract_section(markdown: str, section_id: str, sections: List[Dict]) -> str:
    """Extract a specific section from the markdown."""
    lines = markdown.split('\n')
    
    # Find the target section
    target_section = None
    for s in sections:
        if s['id'] == section_id:
            target_section = s
            break
    
    if not target_section:
        return markdown  # Return full doc if section not found
    
    start_line = target_section['line'] - 1  # 0-indexed
    target_level = target_section['level']
    
    # Find end of section (next heading of same or higher level)
    end_line = len(lines)
    for s in sections:
        if s['line'] > target_section['line'] and s['level'] <= target_level:
            end_line = s['line'] - 1
            break
    
    return '\n'.join(lines[start_line:end_line])


@router.get("/{doc_name}/section/{section_id}")
async def get_section(doc_name: str, section_id: str):
    """Get a specific section of a document."""
    return await get_document(doc_name, section=section_id)


# =============================================================================
# LIVING DOCUMENTATION EVIDENCE SYSTEM
# =============================================================================
# Unified approach for fetching live evidence from this installation's logs.
# 
# Directive format in Markdown: <!-- LOGS: source | filter: "pattern" -->
# 
# Sources map to log files and have predefined filter patterns.
# The frontend calls /api/living-docs/evidence/{source}/{filter} to fetch.
# =============================================================================

# Evidence sources: maps source name to (log_file, service_name_for_journalctl)
# Updated for v6.5.0 data locations and new modules
EVIDENCE_SOURCES = {
    "bootstrap": ("/var/log/hf-timestd/core-recorder.log", "timestd-core-recorder"),
    "fusion": ("/var/log/hf-timestd/fusion.log", "timestd-fusion"),
    "physics": ("/var/log/hf-timestd/physics.log", "timestd-physics"),
    "TEC": ("/var/log/hf-timestd/physics.log", "timestd-physics"),
    "TID": ("/var/log/hf-timestd/physics.log", "timestd-physics"),
    "L1-L2": ("/var/log/hf-timestd/l2-calibration.log", "timestd-l2-calibration"),
    "metrology": ("/var/log/hf-timestd/metrology.log", "timestd-metrology"),
    "arrival_matrix": ("/var/log/hf-timestd/core-recorder.log", "timestd-core-recorder"),
    "consistency": ("/var/log/hf-timestd/physics.log", "timestd-physics"),
    "reanalysis": (None, "timestd-iono-reanalysis"),
}

# Predefined filter patterns for each source
# Format: { "source": { "filter_name": "regex_pattern" } }
EVIDENCE_PATTERNS = {
    "bootstrap": {
        "geographic_expectations": r"\[BOOTSTRAP\] Geographic expectations computed.*|\s+\w+:.*delay=",
        "multi_station_detection": r"\[BOOTSTRAP\] Clustering:.*SNR",
        "recurring_clusters": r"\[BOOTSTRAP\] RECURRING CLUSTERS FOUND",
        "cluster_lock": r"\[BOOTSTRAP\] CLUSTER LOCK:",
        "state_transitions": r"→ CORRELATING|→ TRACKING|→ LOCKED",
        "rtp_lock": r"RTP-to-Unix reference LOCKED",
        "detector_creation": r"\[BOOTSTRAP_SERVICE\] Created ToneDetector",
        # Two-tier bootstrap (v5.3.10)
        "PROVISIONAL LOCK": r"PROVISIONAL LOCK|TIER 1",
        "TIER 2 REFINED LOCK": r"TIER 2 REFINED LOCK|Refined Lock achieved",
        "offset measurements": r"Collected.*offset measurements|offset measurements for refined",
        "Offset change from provisional": r"Offset change from provisional",
        "Station distribution": r"Station distribution:",
        # v6.4 NTP-based time confirmation
        "NTP confirmation": r"time_confirmed=True|BOOTSTRAP_REF.*time_confirmed|Wrote timing reference.*time_confirmed",
        "time_snap": r"time_snap|system_time|start_system_time|Bootstrap offset.*ref_rtp",
    },
    "fusion": {
        "uncertainty": r"uncertainty|σ|sigma|±",
        "kalman": r"[Kk]alman|state|offset",
        "chrony": r"[Cc]hrony|SHM|TSL",
        "convergence": r"converg|settled|stable",
    },
    "physics": {
        "sunrise": r"sunrise|sunset|terminator|solar",
        "tec": r"TEC|TECU|ionospher",
        "propagation": r"propagation|delay|path",
    },
    "TEC": {
        "dispersion": r"dispersion|1/f|frequency|ratio",
        "vtec": r"VTEC|vertical|slant",
        "ionex": r"IONEX|map|grid",
        "estimate": r"TEC.*estimate|estimated TEC|TECU",
    },
    "TID": {
        "detection": r"TID.*detect|Traveling Ionospheric|disturbance",
        "correlation": r"cross.*correlation|path.*correlation|TID.*correlation",
        "velocity": r"TID.*velocity|propagation.*velocity|m/s",
        "event": r"TID.*event|ionospheric.*event",
    },
    "arrival_matrix": {
        "prediction": r"ArrivalPatternMatrix|expected.*arrival|search.*window",
        "physics_validation": r"physics.*valid|IRI.*model|layer.*height",
        "search_window": r"search.*window|±.*σ|sigma",
    },
    "consistency": {
        "validation": r"TimingConsistencyValidator|consistency.*check|constraint",
        "multi_constraint": r"multi.*constraint|cross.*station|emission.*time",
        "physics_check": r"physics.*check|arrival.*sequence|TEC.*reasonable",
    },
    "L1-L2": {
        "L1-L2 difference": r"L1.*L2|difference|calibrat|offset",
        "calibration": r"calibrat|adjust|correct",
    },
    "metrology": {
        "detection": r"detect|tone|signal",
        "measurement": r"measure|D_clock|timing",
    },
    "reanalysis": {
        "mode_validation": r"rejected|reclassified|Reanalysis:|mode_physically_valid",
        "muf_estimate": r"MUF|oblique_muf|fof2|foF2|estimated_muf",
        "tec_reanalysis": r"TEC.*TECU|Negative slope|Physical Inconsistency|tec_tecu",
        "solar_physics": r"solar_elev|fof2|Chapman|solar zenith",
        "hourly_summary": r"Reanalysis complete|Hour \\d+:00",
    },
}


class EvidenceResponse(BaseModel):
    """Response containing evidence from logs."""
    source: str
    filter: str
    lines: List[str]
    timestamp: str
    log_file: Optional[str] = None
    installation_location: Optional[str] = None


def _fetch_evidence(source: str, filter_name: str, max_lines: int = 20) -> EvidenceResponse:
    """Fetch evidence from logs for a given source and filter."""
    import subprocess
    from datetime import datetime
    
    # Get source configuration
    if source not in EVIDENCE_SOURCES:
        return EvidenceResponse(
            source=source,
            filter=filter_name,
            lines=[f"Unknown source '{source}'. Available: {list(EVIDENCE_SOURCES.keys())}"],
            timestamp=datetime.utcnow().isoformat() + "Z"
        )
    
    log_file, service_name = EVIDENCE_SOURCES[source]
    
    # Get pattern - either predefined or use filter_name as literal search
    if source in EVIDENCE_PATTERNS and filter_name in EVIDENCE_PATTERNS[source]:
        pattern = EVIDENCE_PATTERNS[source][filter_name]
    else:
        # Use filter_name as a literal search pattern (escape regex special chars)
        pattern = filter_name.replace(".", r"\.").replace("*", r".*")
    
    log_lines = []
    location = None
    
    try:
        # Try log file first
        if log_file:
            cmd = f"tail -10000 {log_file} 2>/dev/null | grep -iE '{pattern}' | tail -{max_lines}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            log_lines = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
        
        # Fall back to journalctl if no results from file
        if not log_lines and service_name:
            cmd = f"journalctl -u {service_name} --no-pager -n 5000 2>/dev/null | grep -iE '{pattern}' | tail -{max_lines}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            log_lines = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
        
        # For bootstrap, try to extract installation location
        if source == "bootstrap":
            loc_cmd = f"tail -10000 {log_file} 2>/dev/null | grep -oP 'receiver at \\(\\K[^)]+' | tail -1"
            loc_result = subprocess.run(loc_cmd, shell=True, capture_output=True, text=True, timeout=5)
            if loc_result.stdout.strip():
                location = loc_result.stdout.strip()
        
        return EvidenceResponse(
            source=source,
            filter=filter_name,
            lines=log_lines if log_lines else [f"No '{filter_name}' evidence found in {source} logs"],
            timestamp=datetime.utcnow().isoformat() + "Z",
            log_file=log_file,
            installation_location=location
        )
    except subprocess.TimeoutExpired:
        return EvidenceResponse(
            source=source,
            filter=filter_name,
            lines=["Timeout fetching logs"],
            timestamp=datetime.utcnow().isoformat() + "Z"
        )
    except Exception as e:
        return EvidenceResponse(
            source=source,
            filter=filter_name,
            lines=[f"Error fetching logs: {str(e)}"],
            timestamp=datetime.utcnow().isoformat() + "Z"
        )


@router.get("/evidence/{source}/{filter_name}", response_model=EvidenceResponse)
async def get_evidence(source: str, filter_name: str, lines: int = 20):
    """
    Fetch live evidence from this installation's logs.
    
    This is the unified endpoint for all Living Documentation evidence.
    
    Args:
        source: Log source (bootstrap, fusion, physics, TEC, L1-L2, metrology)
        filter_name: Predefined filter name or literal search pattern
        lines: Maximum number of log lines to return (default 20)
    
    Examples:
        /api/living-docs/evidence/bootstrap/geographic_expectations
        /api/living-docs/evidence/fusion/uncertainty
        /api/living-docs/evidence/TEC/dispersion
        /api/living-docs/evidence/L1-L2/L1-L2%20difference
    """
    return _fetch_evidence(source, filter_name, lines)


# Keep backward compatibility with old bootstrap-specific endpoint
@router.get("/evidence/bootstrap/{evidence_type}", response_model=EvidenceResponse, include_in_schema=False)
async def get_bootstrap_evidence_legacy(evidence_type: str, lines: int = 20):
    """Legacy endpoint - redirects to unified evidence endpoint."""
    return _fetch_evidence("bootstrap", evidence_type, lines)
