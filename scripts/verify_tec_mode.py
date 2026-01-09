import h5py
import pandas as pd
from datetime import datetime

PATH = '/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_20260109.h5'

try:
    with h5py.File(PATH, 'r', swmr=True) as f:
        print(f"Keys: {list(f.keys())}")
        
        if 'measurements' in f:
            dset = f['measurements']
            print(f"Columns: {dset.dtype.names}")
            print(f"Count: {len(dset)}")
            
            if len(dset) > 0:
                # Read last 20
                data = dset[-20:]
                df = pd.DataFrame(data)
                
                # Decode strings if needed
                for col in ['station', 'propagation_mode', 'timestamp_utc']:
                    if col in df.columns:
                         df[col] = df[col].apply(lambda x: x.decode('utf-8') if isinstance(x, bytes) else x)
                
                print(df[['timestamp_utc', 'station', 'propagation_mode', 'tec_tecu', 'confidence']].to_string())
            else:
                print("No records found.")

except Exception as e:
    print(f"Error: {e}")
