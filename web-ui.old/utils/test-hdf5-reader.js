#!/usr/bin/env node
/**
 * Test script for HDF5 reader utility
 */

import { readL2TimingMeasurements, readL1AChannelObservables } from './hdf5-reader.js';

async function testL2Reader() {
    console.log('Testing L2 Timing Measurements Reader...\n');

    // Test with an actual file
    const testFile = '/var/lib/timestd/phase2/CHU_3330/clock_offset/CHU_3330_timing_measurements_20251225.h5';

    try {
        const result = await readL2TimingMeasurements(testFile, { maxRecords: 5 });

        console.log('✓ Successfully read HDF5 file');
        console.log(`  Source: ${result.source}`);
        console.log(`  Status: ${result.status}`);
        console.log(`  Total records: ${result.statistics.total_records}`);
        console.log(`  Returned records: ${result.statistics.count}`);
        console.log(`  Grade distribution:`, result.grade_distribution);
        console.log(`\n  Statistics:`);
        console.log(`    Min: ${result.statistics.min?.toFixed(3)} ms`);
        console.log(`    Max: ${result.statistics.max?.toFixed(3)} ms`);
        console.log(`    Mean: ${result.statistics.mean?.toFixed(3)} ms`);
        console.log(`    Std: ${result.statistics.std?.toFixed(3)} ms`);

        console.log(`\n  Sample measurements (first 3):`);
        result.measurements.slice(0, 3).forEach((m, i) => {
            console.log(`    ${i + 1}. ${m.timestamp}`);
            console.log(`       Clock offset: ${m.clock_offset_ms.toFixed(3)} ± ${m.uncertainty_ms.toFixed(3)} ms`);
            console.log(`       Quality: ${m.quality_grade} (${m.quality_flag}), Confidence: ${m.confidence.toFixed(3)}`);
            console.log(`       Station: ${m.station}, Method: ${m.discrimination_method}`);
        });

        return true;
    } catch (error) {
        console.error('✗ Error reading L2 file:', error.message);
        return false;
    }
}

async function testL1AReader() {
    console.log('\n\nTesting L1A Channel Observables Reader...\n');

    // Test with an actual file
    const testFile = '/var/lib/timestd/phase2/CHU_3330/carrier_power/CHU_3330_channel_observables_20251225.h5';

    try {
        const result = await readL1AChannelObservables(testFile, { maxRecords: 5 });

        console.log('✓ Successfully read HDF5 file');
        console.log(`  Source: ${result.source}`);
        console.log(`  Status: ${result.status}`);
        console.log(`  Total records: ${result.total_records}`);
        console.log(`  Returned records: ${result.count}`);

        console.log(`\n  Sample records (first 3):`);
        result.records.slice(0, 3).forEach((r, i) => {
            console.log(`    ${i + 1}. ${r.timestamp}`);
            console.log(`       Quality: ${r.quality_flag}, Completeness: ${(r.data_completeness * 100).toFixed(1)}%`);
            if (r.carrier_power_db !== undefined) {
                console.log(`       Carrier power: ${r.carrier_power_db.toFixed(2)} dB`);
            }
            if (r.carrier_snr_db !== undefined) {
                console.log(`       SNR: ${r.carrier_snr_db.toFixed(2)} dB`);
            }
            if (r.carrier_doppler_hz !== undefined) {
                console.log(`       Doppler: ${r.carrier_doppler_hz.toFixed(3)} Hz`);
            }
        });

        return true;
    } catch (error) {
        console.error('✗ Error reading L1A file:', error.message);
        return false;
    }
}

async function main() {
    console.log('='.repeat(60));
    console.log('HDF5 Reader Utility Test');
    console.log('='.repeat(60));

    const l2Success = await testL2Reader();
    const l1aSuccess = await testL1AReader();

    console.log('\n' + '='.repeat(60));
    console.log('Test Results:');
    console.log(`  L2 Reader: ${l2Success ? '✓ PASS' : '✗ FAIL'}`);
    console.log(`  L1A Reader: ${l1aSuccess ? '✓ PASS' : '✗ FAIL'}`);
    console.log('='.repeat(60));

    process.exit(l2Success && l1aSuccess ? 0 : 1);
}

main();
