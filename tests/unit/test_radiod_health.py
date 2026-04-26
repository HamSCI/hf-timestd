"""
Unit tests for hf_timestd.radiod_health

RadiodHealthChecker probes ka9q-radio's mDNS/multicast status to detect
restarts and verify that specific SSRC channels still exist.
"""

from unittest.mock import patch

import pytest

from hf_timestd.radiod_health import RadiodHealthChecker


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    def test_stores_status_address_and_port(self):
        c = RadiodHealthChecker('239.192.152.141', status_port=5006)
        assert c.status_address == '239.192.152.141'
        assert c.status_port == 5006

    def test_default_port(self):
        c = RadiodHealthChecker('239.0.0.1')
        assert c.status_port == 5006


# =============================================================================
# is_radiod_alive
# =============================================================================


class TestIsRadiodAlive:
    def test_alive_when_discovery_returns_channels(self):
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   return_value={0x1: 'WWV', 0x2: 'CHU'}):
            assert c.is_radiod_alive() is True

    def test_alive_when_discovery_returns_empty_dict(self):
        # An empty dict is still a successful response
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   return_value={}):
            assert c.is_radiod_alive() is True

    def test_dead_when_discovery_raises(self):
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   side_effect=RuntimeError("network unreachable")):
            assert c.is_radiod_alive() is False


# =============================================================================
# verify_channel_exists
# =============================================================================


class TestVerifyChannelExists:
    def test_returns_true_when_ssrc_present(self):
        c = RadiodHealthChecker('239.0.0.1')
        channels = {0x1234: 'WWV_10000', 0x5678: 'CHU_3330'}
        with patch('hf_timestd.radiod_health.discover_channels',
                   return_value=channels):
            assert c.verify_channel_exists(0x1234) is True

    def test_returns_false_when_ssrc_absent(self):
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   return_value={0x1234: 'WWV_10000'}):
            assert c.verify_channel_exists(0xDEAD) is False

    def test_returns_false_when_discovery_fails(self):
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   side_effect=OSError("EHOSTUNREACH")):
            assert c.verify_channel_exists(0x1234) is False


# =============================================================================
# get_status
# =============================================================================


class TestGetStatus:
    def test_alive_status_shape(self):
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   return_value={0x1: 'WWV'}):
            s = c.get_status()
        assert s['radiod_alive'] is True
        assert s['status_address'] == '239.0.0.1'
        assert s['error'] is None
        assert 'check_time' in s
        assert 'check_time_str' in s
        # check_time_str is ISO-8601 formatted
        assert 'T' in s['check_time_str']

    def test_dead_status_carries_error(self):
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   side_effect=Exception("timeout")):
            s = c.get_status()
        assert s['radiod_alive'] is False
        assert s['error'] == 'No status packets received'

    def test_check_time_increases(self):
        c = RadiodHealthChecker('239.0.0.1')
        with patch('hf_timestd.radiod_health.discover_channels',
                   return_value={}):
            t1 = c.get_status()['check_time']
            t2 = c.get_status()['check_time']
        assert t2 >= t1
