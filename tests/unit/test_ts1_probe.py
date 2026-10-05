"""scripts/ts1-probe.sh parses BOTH TS-1 console dialects.

v2.4 firmware (AC0G-ND, 2026-10-05) prints "GPS lock, Satellites in view: 11",
"ref (external) : 27,000,000 Hz" and one "out[N]:" line per Si5351 output.  The
v1.x patterns matched none of it: a locked unit read TS1_GPS_LOCK=no and gave
no TS1_TX_HZ, so setup-station could never compute the injected alias and T6
auto-arm silently did nothing.  The v2.4 fixture is the real transcript.
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / 'scripts' / 'ts1-probe.sh'
FIXTURES = REPO / 'tests' / 'fixtures' / 'ts1'
V24 = FIXTURES / 'stat-v2.4-ac0g-nd-20261005.txt'

# v1.x wording, as recorded from B4 and AI6VN (2026-09-18).
V1X = ("TimeSync v1.8, Board ID # 1234abcd\r\n"
       "Mode: PPS\r\n"
       "GPS locked, 9 satellites in view\r\n"
       "Reference clock(external): 27,000,000 Hz\r\n"
       "Output frequency: 84,225,000.000000 Hz\r\n")


def probe(text: str, tmp_path: Path, adc_hz: str = '129600000') -> dict:
    f = tmp_path / 'stat.txt'
    f.write_text(text)
    r = subprocess.run(['bash', str(PROBE), adc_hz],
                       env={'PATH': '/usr/bin:/bin', 'TS1_PROBE_STAT_FILE': str(f)},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return dict(l.split('=', 1) for l in r.stdout.splitlines() if '=' in l)


def test_v24_real_transcript(tmp_path):
    kv = probe(V24.read_text(), tmp_path)
    assert kv['TS1_GPS_LOCK'] == 'yes'
    assert kv['TS1_SATS'] == '11'
    assert kv['TS1_REF_HZ'] == '27000000'
    assert kv['TS1_REF_SOURCE'] == 'external'
    assert kv['TS1_TX_HZ'] == '84225000'
    assert kv['TS1_REF_OUT_HZ'] == '27000000'
    assert kv['TS1_INJECTED_HZ'] == '45375000'      # the designed alias at 129.6 Msps


def test_v24_alias_at_64_8_msps(tmp_path):
    assert probe(V24.read_text(), tmp_path, '64800000')['TS1_INJECTED_HZ'] == '19425000'


def test_v1x_still_parses(tmp_path):
    kv = probe(V1X, tmp_path)
    assert kv['TS1_GPS_LOCK'] == 'yes'
    assert kv['TS1_SATS'] == '9'
    assert kv['TS1_REF_HZ'] == '27000000'
    assert kv['TS1_REF_SOURCE'] == 'external'
    assert kv['TS1_TX_HZ'] == '84225000'
    assert kv['TS1_FIRMWARE'].startswith('TimeSync v1.8')


def test_internal_reference_is_reported(tmp_path):
    # REF IN dead -> the TS-1 falls back to its internal 10 MHz (AI6VN 09-18).
    text = V24.read_text().replace('ref (external) : 27,000,000 Hz',
                                   'ref (internal) : 10,000,000 Hz')
    kv = probe(text, tmp_path)
    assert kv['TS1_REF_SOURCE'] == 'internal'
    assert kv['TS1_REF_HZ'] == '10000000'


@pytest.mark.parametrize('line', ['No GPS lock, Satellites in view: 0',
                                  'GPS not locked, 0 satellites in view',
                                  'GPS unlocked'])
def test_no_lock_is_never_read_as_lock(tmp_path, line):
    text = V24.read_text().replace('GPS lock, Satellites in view: 11', line)
    assert probe(text, tmp_path)['TS1_GPS_LOCK'] == 'no'
