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


# Bootstrap evidence patterns for Living Documentation
BOOTSTRAP_EVIDENCE_PATTERNS = {
    "geographic_expectations": r"\[BOOTSTRAP\] Geographic expectations computed.*|\s+\w+:.*delay=",
    "multi_station_detection": r"\[BOOTSTRAP\] Clustering:.*SNR",
    "recurring_clusters": r"\[BOOTSTRAP\] RECURRING CLUSTERS FOUND",
    "cluster_lock": r"\[BOOTSTRAP\] CLUSTER LOCK:",
    "state_transitions": r"→ CORRELATING|→ TRACKING|→ LOCKED",
    "rtp_lock": r"RTP-to-Unix reference LOCKED",
    "detector_creation": r"\[BOOTSTRAP_SERVICE\] Created ToneDetector",
}


class BootstrapEvidenceResponse(BaseModel):
    """Response containing bootstrap evidence from logs."""
    evidence_type: str
    lines: List[str]
    timestamp: str
    installation_location: Optional[str] = None


@router.get("/evidence/bootstrap/{evidence_type}", response_model=BootstrapEvidenceResponse)
async def get_bootstrap_evidence(evidence_type: str, lines: int = 20):
    """
    Fetch live bootstrap evidence from this installation's logs.
    
    Evidence types:
    - geographic_expectations: Propagation delay calculations for receiver location
    - multi_station_detection: Multi-station candidate clustering
    - recurring_clusters: 60-second recurrence validation
    - cluster_lock: State transition to CORRELATING
    - state_transitions: All state machine transitions
    - rtp_lock: Final RTP-to-UTC offset lock
    - detector_creation: Per-channel tone detector initialization
    """
    import subprocess
    from datetime import datetime
    
    if evidence_type not in BOOTSTRAP_EVIDENCE_PATTERNS:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Unknown evidence type. Available: {list(BOOTSTRAP_EVIDENCE_PATTERNS.keys())}"
        )
    
    pattern = BOOTSTRAP_EVIDENCE_PATTERNS[evidence_type]
    
    # Fetch from log file with grep
    LOG_FILE = "/var/log/hf-timestd/core-recorder.log"
    try:
        # Get recent logs from core-recorder log file
        cmd = f"tail -10000 {LOG_FILE} 2>/dev/null | grep -E '{pattern}' | tail -{lines}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        log_lines = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
        
        # Try to extract installation location from geographic expectations
        location = None
        if evidence_type == "geographic_expectations" or not log_lines:
            loc_cmd = f"tail -10000 {LOG_FILE} 2>/dev/null | grep -oP 'receiver at \\(\\K[^)]+' | tail -1"
            loc_result = subprocess.run(loc_cmd, shell=True, capture_output=True, text=True, timeout=5)
            if loc_result.stdout.strip():
                location = loc_result.stdout.strip()
        
        return BootstrapEvidenceResponse(
            evidence_type=evidence_type,
            lines=log_lines if log_lines else ["No evidence found - bootstrap may not have run yet"],
            timestamp=datetime.utcnow().isoformat() + "Z",
            installation_location=location
        )
    except subprocess.TimeoutExpired:
        return BootstrapEvidenceResponse(
            evidence_type=evidence_type,
            lines=["Timeout fetching logs"],
            timestamp=datetime.utcnow().isoformat() + "Z"
        )
    except Exception as e:
        return BootstrapEvidenceResponse(
            evidence_type=evidence_type,
            lines=[f"Error fetching logs: {str(e)}"],
            timestamp=datetime.utcnow().isoformat() + "Z"
        )
