"""
Virtual environment check for hf-timestd.

Import this at the top of any script entry point to ensure
the venv is being used:

    from hf_timestd.venv_check import require_venv
    require_venv()
"""

import sys
import os
from pathlib import Path


def in_venv() -> bool:
    """Check if running inside a virtual environment."""
    return (
        hasattr(sys, 'real_prefix') or  # virtualenv
        (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix) or  # venv
        os.environ.get('VIRTUAL_ENV') is not None
    )


def require_venv(exit_on_fail: bool = True) -> bool:
    """
    Require that the script is running in a virtual environment.
    
    Args:
        exit_on_fail: If True, exit with error if not in venv.
                      If False, just print warning and return False.
    
    Returns:
        True if in venv, False otherwise (only if exit_on_fail=False)
    """
    if in_venv():
        return True
    
    # Find the expected venv path
    project_dir = Path(__file__).parent.parent.parent.parent
    venv_path = project_dir / "venv"
    
    msg = f"""
❌ ERROR: hf-timestd must be run from its virtual environment.

Current Python: {sys.executable}
Expected venv:  {venv_path}

To fix, either:
  1. Activate the venv:
     source {venv_path}/bin/activate
     
  2. Or run directly with venv python:
     {venv_path}/bin/python -m <module>
"""
    
    print(msg, file=sys.stderr)
    
    if exit_on_fail:
        sys.exit(1)
    return False


def warn_if_not_venv() -> None:
    """Print a warning if not in venv, but don't exit."""
    require_venv(exit_on_fail=False)
