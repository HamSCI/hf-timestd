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
