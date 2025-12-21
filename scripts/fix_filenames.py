
import os
import re
import glob

root = '/var/lib/timestd/phase2'
files = glob.glob(f'{root}/*/*/*.csv')
# Matches SHARED_2500_2500_... or WWV_20000_20000_...
pattern = re.compile(r'([A-Z]+)_(\d+)_(\d+)_')

count = 0
for f in files:
    filename = os.path.basename(f)
    match = pattern.match(filename)
    if match:
        prefix, freq1, freq2 = match.groups()
        if freq1 == freq2:
            # Replace SHARED_2500_2500 with SHARED_2500
            target = f'{prefix}_{freq1}_{freq2}_'
            replacement = f'{prefix}_{freq1}_'
            new_name = filename.replace(target, replacement)
            new_path = os.path.join(os.path.dirname(f), new_name)
            print(f"Renaming {f} -> {new_path}")
            os.rename(f, new_path)
            count += 1
print(f"Fixed {count} files.")
