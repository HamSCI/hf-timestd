#!/usr/bin/env python3
"""
Command Line Interface for hf-timestd
"""

import sys
import logging
import argparse
from .core.core_recorder_v2 import CoreRecorderV2

def main():
    """Main entry point for hf-timestd command"""
    # Configure logging to show INFO level and above
    # Force level on root logger in case it was already configured
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # Add handler if none exists
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
        root_logger.addHandler(handler)
    else:
        # Set level on existing handlers too
        for handler in root_logger.handlers:
            handler.setLevel(logging.INFO)
    
    # Test that INFO logging works
    logging.info("✓ Logging configured at INFO level")
    
    parser = argparse.ArgumentParser(
        description='hf-timestd',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Create subparsers for different commands
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # Daemon command
    daemon_parser = subparsers.add_parser('daemon', help='Run recorder daemon')
    daemon_parser.add_argument('--config', '-c', help='Configuration file path')
    daemon_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # Discover command
    discover_parser = subparsers.add_parser('discover', help='Discover available channels')
    discover_parser.add_argument('--config', '-c', help='Configuration file path')
    discover_parser.add_argument('--radiod', '-r', help='RadioD address for discovery')
    discover_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # Create channels command
    create_parser = subparsers.add_parser('create-channels', help='Create channels in radiod')
    create_parser.add_argument('--config', '-c', help='Configuration file path')
    create_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # Data management command
    data_parser = subparsers.add_parser('data', help='Manage recorded data')
    data_subparsers = data_parser.add_subparsers(dest='data_command', help='Data management command')
    
    # Data summary
    summary_parser = data_subparsers.add_parser('summary', help='Show data storage summary')
    summary_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                               help='Configuration file path')
    summary_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean data
    clean_data_parser = data_subparsers.add_parser('clean-data', help='Delete RTP recordings')
    clean_data_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                   help='Configuration file path')
    clean_data_parser.add_argument('--dry-run', action='store_true',
                                   help='Show what would be deleted without deleting')
    clean_data_parser.add_argument('--yes', '-y', action='store_true',
                                   help='Skip confirmation prompts')
    clean_data_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean analytics
    clean_analytics_parser = data_subparsers.add_parser('clean-analytics', 
                                                         help='Delete analytics (can be regenerated)')
    clean_analytics_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                        help='Configuration file path')
    clean_analytics_parser.add_argument('--dry-run', action='store_true',
                                        help='Show what would be deleted without deleting')
    clean_analytics_parser.add_argument('--yes', '-y', action='store_true',
                                        help='Skip confirmation prompts')
    clean_analytics_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean uploads
    clean_uploads_parser = data_subparsers.add_parser('clean-uploads', help='Clear upload queue')
    clean_uploads_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                      help='Configuration file path')
    clean_uploads_parser.add_argument('--dry-run', action='store_true',
                                      help='Show what would be deleted without deleting')
    clean_uploads_parser.add_argument('--yes', '-y', action='store_true',
                                      help='Skip confirmation prompts')
    clean_uploads_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean all
    clean_all_parser = data_subparsers.add_parser('clean-all', 
                                                   help='Delete all RTP data, analytics, and uploads')
    clean_all_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                  help='Configuration file path')
    clean_all_parser.add_argument('--dry-run', action='store_true',
                                  help='Show what would be deleted without deleting')
    clean_all_parser.add_argument('--yes', '-y', action='store_true',
                                  help='Skip confirmation prompts')
    clean_all_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # GRAPE command group
    grape_parser = subparsers.add_parser('grape', help='GRAPE data products (decimation, spectrograms, packaging)')
    grape_subparsers = grape_parser.add_subparsers(dest='grape_command', help='GRAPE command')
    
    # GRAPE decimate
    grape_decimate_parser = grape_subparsers.add_parser('decimate', help='Decimate 24/20 kHz IQ to 10 Hz')
    grape_decimate_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_decimate_parser.add_argument('--channel', help='Channel name (e.g., "WWV 10 MHz")')
    grape_decimate_parser.add_argument('--date', help='Date (YYYY-MM-DD or YYYYMMDD)')
    grape_decimate_parser.add_argument('--all-channels', action='store_true', help='Process all channels')
    grape_decimate_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE spectrogram
    grape_spec_parser = grape_subparsers.add_parser('spectrogram', help='Generate carrier spectrograms')
    grape_spec_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_spec_parser.add_argument('--channel', required=True, help='Channel name')
    grape_spec_parser.add_argument('--date', help='Date (YYYY-MM-DD or YYYYMMDD)')
    grape_spec_parser.add_argument('--rolling', type=int, choices=[6, 12, 24], help='Rolling spectrogram (hours)')
    grape_spec_parser.add_argument('--grid', help='Receiver grid square for solar zenith overlay')
    grape_spec_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE package
    grape_package_parser = grape_subparsers.add_parser('package', help='Package as Digital RF for upload')
    grape_package_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_package_parser.add_argument('--date', required=True, help='Date to package')
    grape_package_parser.add_argument('--callsign', required=True, help='Station callsign')
    grape_package_parser.add_argument('--grid', required=True, help='Grid square')
    grape_package_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE upload
    grape_upload_parser = grape_subparsers.add_parser('upload', help='Upload to PSWS repository')
    grape_upload_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_upload_parser.add_argument('--date', help='Date to upload (default: yesterday)')
    grape_upload_parser.add_argument('--dry-run', action='store_true', help='Show what would be uploaded')
    grape_upload_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE status
    grape_status_parser = grape_subparsers.add_parser('status', help='Show upload status and history')
    grape_status_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_status_parser.add_argument('--days', type=int, default=7, help='Days of history to show')
    grape_status_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    args = parser.parse_args()
    
    # If no command specified, show help
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Update logging level if debug flag is set
    if hasattr(args, 'debug') and args.debug:
        root_logger.setLevel(logging.DEBUG)
        for handler in root_logger.handlers:
            handler.setLevel(logging.DEBUG)
        logging.info("DEBUG logging enabled")
    
    # Handle commands
    if args.command == 'daemon':
        import toml
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            print(f"   Use --config to specify a different file")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)

        # Build config for CoreRecorder
        # Determine output directory based on mode
        recorder_section = config.get('recorder', {})
        mode = recorder_section.get('mode', 'test')
        
        if mode == 'test':
            output_dir = recorder_section.get('test_data_root', '/tmp/timestd-test')
        else:
            output_dir = recorder_section.get('production_data_root', '/var/lib/signal-recorder')
        
        recorder_config = {
            'multicast_address': config.get('ka9q', {}).get('data_address', '239.103.26.231'),
            'port': 5004,
            'output_dir': output_dir,
            'station': config.get('station', {}),
            'channels': recorder_section.get('channels', []),
            'status_address': config.get('ka9q', {}).get('status_address', '239.192.152.141')
        }
        
        # Start daemon mode
        recorder = CoreRecorderV2(recorder_config)
        recorder.run()
    elif args.command == 'discover':
        import toml
        from .channel_manager import ChannelManager
        
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)
        
        # Discovery mode
        status_address = args.radiod or config.get('ka9q', {}).get('status_address', '239.192.152.141')
        manager = ChannelManager(status_address)
        channels = manager.discover_channels()
        
        print(f"\n📡 Discovered {len(channels)} channels from radiod at {status_address}:")
        for ch in channels:
            print(f"  • SSRC {ch['ssrc']:08x}: {ch.get('frequency_hz', 0)/1e6:.3f} MHz - {ch.get('description', 'Unknown')}")
    elif args.command == 'create-channels':
        import toml
        from .channel_manager import ChannelManager
        
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)
        
        # Create channels mode
        status_address = config.get('ka9q', {}).get('status_address', '239.192.152.141')
        manager = ChannelManager(status_address)
        
        # Build channel specifications
        required_channels = []
        for ch_cfg in config.get('recorder', {}).get('channels', []):
            if ch_cfg.get('enabled', True):
                required_channels.append({
                    'ssrc': ch_cfg['ssrc'],
                    'frequency_hz': ch_cfg['frequency_hz'],
                    'preset': ch_cfg.get('preset', 'iq'),
                    'sample_rate': ch_cfg.get('sample_rate', 16000),
                    'agc': ch_cfg.get('agc', 0),
                    'gain': ch_cfg.get('gain', 0),
                    'description': ch_cfg['description']
                })
        
        if not required_channels:
            print("❌ No enabled channels found in configuration")
            sys.exit(1)
        
        print(f"\n🔧 Creating {len(required_channels)} channels in radiod at {status_address}...")
        success = manager.ensure_channels_exist(required_channels, update_existing=False)
        
        if success:
            print("✅ All channels created successfully")
        else:
            print("⚠️ Some channels may have failed to create")
            sys.exit(1)
    elif args.command == 'data':
        # Data management mode
        from .data_management import DataManager
        from .config_utils import load_config_with_paths
        import toml
        
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            print(f"   Use --config to specify a different file")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)
        
        # Create path resolver
        from .config_utils import PathResolver
        path_resolver = PathResolver(config, development_mode=args.dev)
        
        # Create data manager
        manager = DataManager(path_resolver)
        
        # Execute data command
        if args.data_command == 'summary':
            manager.print_data_summary()
        elif args.data_command == 'clean-data':
            manager.clean_data(dry_run=args.dry_run, confirm=args.yes)
        elif args.data_command == 'clean-analytics':
            manager.clean_analytics(dry_run=args.dry_run, confirm=args.yes)
        elif args.data_command == 'clean-uploads':
            manager.clean_uploads(dry_run=args.dry_run, confirm=args.yes)
        elif args.data_command == 'clean-all':
            manager.clean_all(dry_run=args.dry_run, confirm=args.yes)
        else:
            data_parser.print_help()
            sys.exit(1)
    elif args.command == 'grape':
        # GRAPE data products mode
        from pathlib import Path
        from datetime import datetime, timedelta
        
        if not args.grape_command:
            grape_parser.print_help()
            sys.exit(1)
        
        data_root = Path(args.data_root)
        
        if args.grape_command == 'decimate':
            from .grape.decimation_pipeline import DecimationPipeline
            
            # Handle date format
            if args.date:
                date_str = args.date.replace('-', '')
            else:
                date_str = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            
            pipeline = DecimationPipeline(data_root)
            
            if args.all_channels:
                # Get all channels from raw_archive
                channels_dir = data_root / 'raw_archive'
                if channels_dir.exists():
                    for channel_dir in channels_dir.iterdir():
                        if channel_dir.is_dir():
                            channel_name = channel_dir.name.replace('_', ' ')
                            print(f"Processing {channel_name}...")
                            pipeline.process_day(date_str, channel_name)  # FIXED: date first, then channel
                else:
                    print(f"❌ No raw_archive found at {channels_dir}")
                    sys.exit(1)
            elif args.channel:
                pipeline.process_day(date_str, args.channel)  # FIXED: date first, then channel
            else:
                print("❌ Specify --channel or --all-channels")
                sys.exit(1)

                
        elif args.grape_command == 'spectrogram':
            from .grape.spectrogram import CarrierSpectrogramGenerator
            import toml
            
            # Get grid from args or config file
            receiver_grid = args.grid
            if not receiver_grid:
                config_path = Path('/etc/hf-timestd/timestd-config.toml')
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        config = toml.load(f)
                    receiver_grid = config.get('station', {}).get('grid_square', '')
                    if receiver_grid:
                        print(f"Using grid from config: {receiver_grid}")
            
            gen = CarrierSpectrogramGenerator(
                data_root=data_root,
                channel_name=args.channel,
                receiver_grid=receiver_grid or ''
            )
            
            if args.date:
                date_str = args.date.replace('-', '')
                gen.generate_daily(date_str)
            elif args.rolling:
                gen.generate_rolling(hours=args.rolling)
            else:
                # Default to yesterday
                date_str = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
                gen.generate_daily(date_str)
                
        elif args.grape_command == 'package':
            from .grape.packager import DailyDRFPackager, StationConfig
            
            date_str = args.date.replace('-', '')
            station_config = StationConfig(
                callsign=args.callsign,
                grid_square=args.grid
            )
            packager = DailyDRFPackager(
                data_root=data_root,
                station_config=station_config
            )
            packager.package_day(date_str)
            
        elif args.grape_command == 'upload':
            from .grape.uploader import UploadManager, SFTPUpload
            import toml
            
            if args.date:
                date_str = args.date.replace('-', '')
            else:
                date_str = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            
            # Load config for station info
            config_path = Path('/etc/hf-timestd/timestd-config.toml')
            if config_path.exists():
                with open(config_path, 'r') as f:
                    config = toml.load(f)
            else:
                print(f"❌ Config not found: {config_path}")
                sys.exit(1)
            
            station = config.get('station', {})
            
            # Find packaged data for the date
            upload_dir = data_root / 'upload' / date_str
            if not upload_dir.exists():
                print(f"❌ No packaged data for {date_str} at {upload_dir}")
                print(f"   Run 'grape package --date {date_str}' first")
                sys.exit(1)
            
            # Find OBS directories
            obs_dirs = list(upload_dir.rglob('OBS*'))
            if not obs_dirs:
                print(f"❌ No OBS directories found in {upload_dir}")
                sys.exit(1)
            
            print(f"📤 Upload for {date_str}")
            print(f"   Found {len(obs_dirs)} dataset(s)")
            
            if args.dry_run:
                print("   (Dry run - no actual upload)")
                for obs_dir in obs_dirs:
                    print(f"   Would upload: {obs_dir}")
                sys.exit(0)
            
            # Create uploader from config
            uploader_config = config.get('uploader', {})
            sftp_config = uploader_config.get('sftp', {})
            
            # Expand ~ in ssh_key path
            import os
            ssh_key = os.path.expanduser(sftp_config.get('ssh_key', '~/.ssh/psws_key'))
            
            upload_config = {
                'protocol': uploader_config.get('protocol', 'sftp'),
                'host': sftp_config.get('host', 'pswsnetwork.eng.ua.edu'),
                'user': sftp_config.get('user', station.get('id', '')),
                'ssh': {'key_file': ssh_key},
                'bandwidth_limit_kbps': sftp_config.get('bandwidth_limit_kbps', 100),
                'max_retries': uploader_config.get('max_retries', 5),
                'queue_file': data_root / 'upload' / 'queue.json'
            }
            
            manager = UploadManager(upload_config)
            
            # Enqueue and process
            for obs_dir in obs_dirs:
                metadata = {
                    'date': f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                    'callsign': station.get('callsign', 'AC0G'),
                    'grid_square': station.get('grid_square', 'EM38ww'),
                    'station_id': station.get('id', 'S000171'),
                    'instrument_id': station.get('instrument_id', '172')
                }
                manager.enqueue(obs_dir, metadata)
            
            manager.process_queue()
            
            status = manager.get_status()
            print(f"   Queue status: {status['completed']} completed, {status['pending']} pending, {status['failed']} failed")
            
            # Write upload report
            report_file = manager.write_upload_report()
            print(f"   Report: {report_file}")
            
        elif args.grape_command == 'status':
            from .grape.uploader import UploadManager
            
            # Create minimal config just to read queue
            upload_config = {
                'protocol': 'sftp',
                'host': 'pswsnetwork.eng.ua.edu',
                'user': 'status_check',
                'ssh': {'key_file': '/dev/null'},
                'queue_file': data_root / 'upload' / 'queue.json'
            }
            
            manager = UploadManager(upload_config)
            
            # Current queue status
            status = manager.get_status()
            print(f"\n📊 GRAPE Upload Status")
            print(f"   Queue: {status['total']} total")
            print(f"   ├─ Completed: {status['completed']}")
            print(f"   ├─ Pending:   {status['pending']}")
            print(f"   ├─ Uploading: {status['uploading']}")
            print(f"   └─ Failed:    {status['failed']}")
            
            # History
            history = manager.get_upload_history(days=args.days)
            if history:
                print(f"\n📅 Upload History (last {args.days} days):")
                for day in history:
                    summary = day.get('summary', {})
                    print(f"   {day['date']}: "
                          f"{summary.get('completed', 0)} completed, "
                          f"{summary.get('failed', 0)} failed")
            else:
                print(f"\n   No upload history found")
        else:
            grape_parser.print_help()
            sys.exit(1)

if __name__ == '__main__':
    main()
