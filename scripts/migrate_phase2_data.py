#!/usr/bin/env python3
import os
import shutil
from pathlib import Path

DATA_ROOT = Path('/var/lib/timestd/phase2')
RAW_ROOT = Path('/var/lib/timestd/raw_buffer')

# Map Old Directory -> New Directory
# Based on existing 'timestd-metrology.sh' and new convention 'Station_kHz'
MAPPING = {
    # WWV Only
    'WWV_20_MHz': 'WWV_20000',
    'WWV_25_MHz': 'WWV_25000',
    # CHU
    'CHU_3.33_MHz': 'CHU_3330',
    'CHU_7.85_MHz': 'CHU_7850',
    'CHU_14.67_MHz': 'CHU_14670',
    # SHARED (WWV/WWVH/BPM)
    # Keeping 'SHARED' prefix as it's the channel name.
    'SHARED_2.5_MHz': 'SHARED_2500',
    'SHARED_5_MHz': 'SHARED_5000',
    'SHARED_10_MHz': 'SHARED_10000',
    'SHARED_15_MHz': 'SHARED_15000'
}

def migrate():
    print(f"Starting migration in {DATA_ROOT} and {RAW_ROOT}")
    
    # 1. Migrate phase2 data
    if DATA_ROOT.exists():
        for old_name, new_name in MAPPING.items():
            old_path = DATA_ROOT / old_name
            new_path = DATA_ROOT / new_name
            if old_path.exists():
                print(f"Migrating {old_path} -> {new_path}")
                _merge_dirs(old_path, new_path)
                # Rename files in the newly moved/merged dir
                _rename_files_in_dir(new_path, old_name, new_name)

    # 2. Migrate raw_buffer data
    if RAW_ROOT.exists():
        for old_name, new_name in MAPPING.items():
            old_path = RAW_ROOT / old_name
            new_path = RAW_ROOT / new_name
            if old_path.exists():
                print(f"Migrating {old_path} -> {new_path}")
                _merge_dirs(old_path, new_path)

def _merge_dirs(src: Path, dst: Path):
    """Recursively move content from src to dst."""
    if not dst.exists():
        src.rename(dst)
        return
    
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _merge_dirs(item, target)
        else:
            if target.exists():
                target.unlink() # Overwrite
            shutil.move(str(item), str(target))
    
    # Remove empty src dir
    if src.exists():
        src.rmdir()

def _rename_files_in_dir(path, old_name, new_name):
    old_prefix_dots = old_name
    old_prefix_underscores = old_name.replace('.', '_')
    for root, dirs, files in os.walk(path):
        for filename in files:
            new_filename = filename
            if filename.startswith(old_prefix_dots):
                new_filename = filename.replace(old_prefix_dots, new_name, 1)
            elif filename.startswith(old_prefix_underscores):
                new_filename = filename.replace(old_prefix_underscores, new_name, 1)
            
            if new_filename != filename:
                print(f"  Renaming {filename} -> {new_filename}")
                os.rename(os.path.join(root, filename), os.path.join(root, new_filename))

    print("Migration complete.")

if __name__ == "__main__":
    migrate()
