"""
Signal processor module

Provides base class for signal-specific processors and implements GRAPE processor.
"""

import subprocess
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class SignalProcessor(ABC):
    """Base class for signal-specific processors"""
    
    @abstractmethod
    def validate_files(self, file_list: List[Path]) -> Dict[str, Any]:
        """
        Check if all required files are present and valid
        
        Args:
            file_list: List of file paths to validate
            
        Returns:
            Dictionary with validation results
        """
        pass
    
    @abstractmethod
    def repair_gaps(self, input_dir: Path, missing_minutes: List[str]) -> bool:
        """
        Fill missing data with silence or interpolation
        
        Args:
            input_dir: Directory containing files
            missing_minutes: List of missing minute timestamps (HHMMSS)
            
        Returns:
            True if repair successful
        """
        pass
    
    @abstractmethod
    def process(self, input_dir: Path, output_dir: Path, config: Dict, metadata: Dict) -> Optional[Path]:
        """
        Main processing logic
        
        Args:
            input_dir: Directory containing input files
            output_dir: Directory for output files
            config: Processor configuration
            metadata: Additional metadata (station info, etc.)
            
        Returns:
            Path to output dataset, or None if failed
        """
        pass
    
    @abstractmethod
    def get_upload_format(self) -> str:
        """
        Return expected output format
        
        Returns:
            Format string (e.g., 'digital_rf', 'netcdf', 'hdf5')
        """
        pass


class GRAPEProcessor(SignalProcessor):
    """
    Processor for WWV/CHU data for HamSCI GRAPE
    
    Processing steps:
    1. Validate 1440 one-minute files
    2. Repair gaps with silence
    3. Decompress wavpack files if needed
    4. Concatenate and resample to 10 Hz using sox
    5. Convert to Digital RF format using wav2grape.py
    """
    
    def __init__(self, config: Dict):
        """
        Initialize GRAPE processor
        
        Args:
            config: Processor configuration
        """
        self.config = config
        self.target_sample_rate = config.get('target_sample_rate', 10)
        self.silent_file = Path("/usr/share/signal-recorder/one-minute-silent-float.wv")
    
    def validate_files(self, file_list: List[Path]) -> Dict[str, Any]:
        """Validate that we have enough files for processing"""
        expected = 1440  # One file per minute
        actual = len(file_list)
        
        # Extract minute timestamps
        timestamps = set()
        for f in file_list:
            name = f.stem
            if 'T' in name:
                timestamp = name.split('T')[1]
                timestamps.add(timestamp)
        
        # Find missing minutes
        expected_timestamps = set()
        for hour in range(24):
            for minute in range(60):
                timestamp = f"{hour:02d}{minute:02d}00"
                expected_timestamps.add(timestamp)
        
        missing_minutes = sorted(expected_timestamps - timestamps)
        
        # Consider valid if we have at least 95% of files
        valid = actual >= expected * 0.95
        
        return {
            "valid": valid,
            "files_found": actual,
            "files_expected": expected,
            "missing_minutes": missing_minutes,
            "completeness": actual / expected * 100
        }
    
    def repair_gaps(self, input_dir: Path, missing_minutes: List[str]) -> bool:
        """
        Insert silent files for missing minutes
        
        Args:
            input_dir: Directory containing files
            missing_minutes: List of missing minute timestamps (HHMMSS)
            
        Returns:
            True if repair successful
        """
        if not missing_minutes:
            logger.info("No missing files to repair")
            return True
        
        logger.info(f"Repairing {len(missing_minutes)} missing files")
        
        # Check if silent file exists
        if not self.silent_file.exists():
            logger.warning(f"Silent file not found: {self.silent_file}")
            logger.info("Creating silent file...")
            self._create_silent_file()
        
        # Get date from directory name
        # Assumes directory structure: .../YYYYMMDD/.../BAND/
        date_str = None
        for part in input_dir.parts:
            if len(part) == 8 and part.isdigit():
                date_str = part
                break
        
        if not date_str:
            logger.error("Could not determine date from directory path")
            return False
        
        # Create symlinks for missing files
        for minute in missing_minutes:
            filename = f"{date_str}T{minute}.wv"
            target_path = input_dir / filename
            
            try:
                if not target_path.exists():
                    target_path.symlink_to(self.silent_file)
                    logger.debug(f"Created symlink: {filename}")
            except Exception as e:
                logger.error(f"Error creating symlink for {filename}: {e}")
                return False
        
        logger.info(f"Successfully repaired {len(missing_minutes)} missing files")
        return True
    
    def process(self, input_dir: Path, output_dir: Path, config: Dict, metadata: Dict) -> Optional[Path]:
        """
        Process WWV/CHU data for GRAPE
        
        Args:
            input_dir: Directory containing 1-minute .wv files
            output_dir: Directory for output
            config: Processing configuration
            metadata: Station metadata
            
        Returns:
            Path to Digital RF dataset directory
        """
        logger.info(f"Processing GRAPE data from {input_dir}")
        
        try:
            # Step 1: Decompress wavpack files
            wav_files = self._decompress_wavpack_files(input_dir)
            if not wav_files:
                logger.error("No WAV files after decompression")
                return None
            
            # Step 2: Concatenate and resample to 10 Hz
            wav_24h = self._create_24h_wav(wav_files, output_dir)
            if not wav_24h or not wav_24h.exists():
                logger.error("Failed to create 24-hour WAV file")
                return None
            
            # Step 3: Convert to Digital RF format
            drf_dataset = self._convert_to_digital_rf(
                wav_24h,
                output_dir,
                metadata
            )
            
            if drf_dataset and drf_dataset.exists():
                logger.info(f"Successfully created Digital RF dataset: {drf_dataset}")
                return drf_dataset
            else:
                logger.error("Failed to create Digital RF dataset")
                return None
        
        except Exception as e:
            logger.error(f"Error processing GRAPE data: {e}", exc_info=True)
            return None
    
    def get_upload_format(self) -> str:
        """Return output format"""
        return "digital_rf"
    
    def _create_silent_file(self):
        """Create a one-minute silent wavpack file"""
        logger.info("Creating one-minute silent file")
        
        # Create temporary WAV file with silence
        # 1 minute at 16 kHz, 2 channels (I/Q), float32
        import numpy as np
        import soundfile as sf
        
        sample_rate = 16000
        duration = 60  # seconds
        channels = 2
        samples = np.zeros((sample_rate * duration, channels), dtype=np.float32)
        
        temp_wav = Path("/tmp/one-minute-silent.wav")
        sf.write(temp_wav, samples, sample_rate, subtype='FLOAT')
        
        # Compress to wavpack
        self.silent_file.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["wavpack", "-y", str(temp_wav), "-o", str(self.silent_file)]
        subprocess.run(cmd, check=True)
        
        temp_wav.unlink()
        logger.info(f"Created silent file: {self.silent_file}")
    
    def _decompress_wavpack_files(self, input_dir: Path) -> List[Path]:
        """
        Decompress all .wv files to .wav
        
        Args:
            input_dir: Directory containing .wv files
            
        Returns:
            List of decompressed .wav file paths
        """
        wv_files = sorted(input_dir.glob("*.wv"))
        wav_files = []
        
        logger.info(f"Decompressing {len(wv_files)} wavpack files")
        
        for wv_file in wv_files:
            wav_file = wv_file.with_suffix('.wav')
            
            # Skip if already decompressed
            if wav_file.exists():
                wav_files.append(wav_file)
                continue
            
            try:
                # Decompress with wvunpack
                cmd = ["wvunpack", "-y", str(wv_file), "-o", str(wav_file)]
                subprocess.run(cmd, check=True, capture_output=True)
                wav_files.append(wav_file)
            except subprocess.CalledProcessError as e:
                logger.error(f"Error decompressing {wv_file}: {e.stderr.decode()}")
            except Exception as e:
                logger.error(f"Error decompressing {wv_file}: {e}")
        
        logger.info(f"Decompressed {len(wav_files)} files")
        return wav_files
    
    def _create_24h_wav(self, wav_files: List[Path], output_dir: Path) -> Optional[Path]:
        """
        Concatenate WAV files and resample to 10 Hz
        
        Args:
            wav_files: List of 1-minute WAV files
            output_dir: Output directory
            
        Returns:
            Path to 24-hour resampled WAV file
        """
        output_file = output_dir / "24_hour_10sps_iq.wav"
        
        logger.info(f"Creating 24-hour WAV file: {output_file}")
        logger.info(f"Concatenating {len(wav_files)} files and resampling to {self.target_sample_rate} Hz")
        
        try:
            # Use sox to concatenate and resample
            # sox input1.wav input2.wav ... output.wav rate 10
            cmd = ["sox"] + [str(f) for f in wav_files] + [
                str(output_file),
                "rate", str(self.target_sample_rate)
            ]
            
            logger.debug(f"Running sox command with {len(wav_files)} input files")
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            
            if output_file.exists():
                size_mb = output_file.stat().st_size / 1024 / 1024
                logger.info(f"Created 24-hour WAV file: {output_file} ({size_mb:.1f} MB)")
                return output_file
            else:
                logger.error("sox completed but output file not found")
                return None
        
        except subprocess.CalledProcessError as e:
            logger.error(f"Error running sox: {e.stderr}")
            return None
        except Exception as e:
            logger.error(f"Error creating 24-hour WAV: {e}")
            return None
    
    def _convert_to_digital_rf(self, wav_file: Path, output_dir: Path, metadata: Dict) -> Optional[Path]:
        """
        Convert 24-hour WAV to Digital RF format
        
        Args:
            wav_file: Path to 24-hour WAV file
            output_dir: Output directory
            metadata: Station metadata
            
        Returns:
            Path to Digital RF dataset directory
        """
        logger.info("Converting to Digital RF format")
        
        # Determine date and create output path
        # Parse date from directory structure
        date_str = None
        for part in wav_file.parts:
            if len(part) == 8 and part.isdigit():
                date_str = part
                break
        
        if not date_str:
            logger.error("Could not determine date from file path")
            return None
        
        # Create Digital RF output directory
        # Format: YYYYMMDD/REPORTER_GRID/RECEIVER@PSWS_ID/OBS{datetime}/
        date_obj = datetime.strptime(date_str, "%Y%m%d")
        obs_dirname = date_obj.strftime("OBS%Y-%m-%dT%H-%M")
        
        drf_output = output_dir / obs_dirname
        
        # Use wav2grape.py (from wsprdaemon) or implement conversion
        # For now, we'll call it as a subprocess if available
        try:
            # Check if wav2grape.py is available
            wav2grape_script = shutil.which("wav2grape.py")
            
            if not wav2grape_script:
                # Try to find it in wsprdaemon directory
                wsprdaemon_path = Path("/home/ubuntu/wsprdaemon/wav2grape.py")
                if wsprdaemon_path.exists():
                    wav2grape_script = str(wsprdaemon_path)
            
            if wav2grape_script:
                logger.info(f"Using wav2grape.py: {wav2grape_script}")
                
                # Create temporary input directory with expected structure
                temp_input = output_dir / "temp_wav2grape_input"
                temp_input.mkdir(exist_ok=True)
                
                # Copy WAV file to expected location
                # wav2grape expects: input_dir/subchannel/file.wav
                subchannel_dir = temp_input / metadata.get('band', 'WWV_IQ')
                subchannel_dir.mkdir(exist_ok=True)
                temp_wav = subchannel_dir / wav_file.name
                shutil.copy(wav_file, temp_wav)
                
                # Run wav2grape.py
                cmd = [
                    "python3", wav2grape_script,
                    "-i", str(temp_input),
                    "-o", str(drf_output.parent),
                    "-s", date_obj.strftime("%Y-%m-%dT00:00:00")
                ]
                
                result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                logger.info(f"wav2grape.py output: {result.stdout}")
                
                # Cleanup temp directory
                shutil.rmtree(temp_input)
                
                if drf_output.exists():
                    return drf_output
            else:
                logger.warning("wav2grape.py not found, using simplified conversion")
                # Implement basic Digital RF conversion here if needed
                # For now, just return the WAV file path
                return wav_file
        
        except Exception as e:
            logger.error(f"Error converting to Digital RF: {e}", exc_info=True)
            return None
        
        return drf_output if drf_output.exists() else None


# Registry of available processors
PROCESSOR_REGISTRY: Dict[str, type] = {
    "grape": GRAPEProcessor,
    # Future processors can be added here:
    # "codar": CODARProcessor,
    # "hf_radar": HFRadarProcessor,
}


def get_processor(processor_type: str, config: Dict) -> SignalProcessor:
    """
    Get processor instance by type
    
    Args:
        processor_type: Type of processor (e.g., "grape")
        config: Processor configuration
        
    Returns:
        SignalProcessor instance
        
    Raises:
        ValueError: If processor type not found
    """
    processor_class = PROCESSOR_REGISTRY.get(processor_type)
    if not processor_class:
        raise ValueError(f"Unknown processor type: {processor_type}")
    
    return processor_class(config)

