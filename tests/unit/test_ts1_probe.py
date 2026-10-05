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

# The "Output frequency" dialect, as recorded from B4 (v2.6) and AI6VN (v2.7),
# 2026-09-18.  The version numbers do not predict the dialect.
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
                                  'GPS unlocked',
                                  'GPS lock lost, Satellites in view: 3',
                                  'Waiting for GPS lock, Satellites in view: 2',
                                  'GPS lock: no, Satellites in view: 0'])
def test_no_lock_is_never_read_as_lock(tmp_path, line):
    text = V24.read_text().replace('GPS lock, Satellites in view: 11', line)
    assert probe(text, tmp_path)['TS1_GPS_LOCK'] == 'no'


def test_help_text_before_stat_cannot_claim_lock(tmp_path):
    # Production sends '?' then STAT; only the STAT reply may decide lock.
    help_ = ("?\r\nCommands:\r\n  STAT  show status (GPS lock, outputs)\r\n"
             "  REF   set reference clock\r\nTS>\r\n")
    text = help_ + V24.read_text().replace('GPS lock, Satellites in view: 11',
                                            'No GPS lock, Satellites in view: 0')
    assert probe(text, tmp_path)['TS1_GPS_LOCK'] == 'no'


def test_a_zero_hz_carrier_is_withheld_not_armed(tmp_path):
    text = V24.read_text().replace('84,225,000.000000 Hz', '0.000000 Hz')
    kv = probe(text, tmp_path)
    assert 'TS1_TX_HZ' not in kv and 'TS1_INJECTED_HZ' not in kv
    assert 'implausible' in kv['TS1_WARN']
    assert 'TS1_ERROR' not in kv          # the TS-1 itself is still usable


def test_a_missing_transcript_is_not_a_present_ts1(tmp_path):
    r = subprocess.run(['bash', str(PROBE)],
                       env={'PATH': '/usr/bin:/bin',
                            'TS1_PROBE_STAT_FILE': str(tmp_path / 'nope.txt')},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert 'TS1_PRESENT=no' in r.stdout


def test_setup_station_never_passes_the_fixture_env():
    sh = (REPO / 'scripts' / 'setup-station.sh').read_text()
    assert 'env -u TS1_PROBE_STAT_FILE "$PROJECT_DIR/scripts/ts1-probe.sh"' in sh


def test_inline_prompt_echo_anchors_the_status(tmp_path):
    # The console prints its prompt before the echo: "TS>STAT".  Help text
    # before it names "GPS lock"; the real status says no lock.
    help_ = "?\r\nCommands:\r\n  STAT  show status (GPS lock, outputs)\r\nTS>"
    text = help_ + V24.read_text().replace('GPS lock, Satellites in view: 11',
                                            'No GPS lock, Satellites in view: 0')
    assert probe(text, tmp_path)['TS1_GPS_LOCK'] == 'no'
    locked = help_ + V24.read_text()
    assert probe(locked, tmp_path)['TS1_GPS_LOCK'] == 'yes'


def test_without_any_echo_a_negative_anywhere_wins(tmp_path):
    text = ("Commands: STAT shows GPS lock\r\nMode: GPS-PPS\r\n"
            "No GPS lock, Satellites in view: 0\r\n")
    assert probe(text, tmp_path)['TS1_GPS_LOCK'] == 'no'


def test_setup_station_surfaces_the_probe_warning():
    sh = (REPO / 'scripts' / 'setup-station.sh').read_text()
    assert "TS1_WARN=" in sh and 'log_warn "TS-1 probe: $_ts1_warn"' in sh
