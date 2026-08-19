"""The shipped WWVB channel must be narrow.

WWVB is a 60 kHz carrier whose time code is one bit per second. A few hundred
Hz of bandwidth is ample; the channel width only sets how much noise is let in
alongside it.

The template shipped 24000 Hz -- six times wider than needed, roughly 7.8 dB of
SNR discarded for nothing -- and that is the sole reason AC0G-B4 could not
decode WWVB. Measured there on 2026-08-18/19, same antenna, same site, same
code, changing only this value:

    24 kHz : 2758 passes,  1 frame decoded (0.036%), 29.2 of 90 s detected
     4 kHz : 1533 passes, 17 frames decoded (1.11%),  89.2 of 90 s detected

A 30x improvement in decode rate, and the second-detector went from seeing a
third of the seconds to essentially all of them. bee1 had been carrying a
hand-edited 4000 for months, which is the only reason that host ever decoded --
the difference was never the antenna.

These assertions exist so a future edit cannot quietly widen the channel again
and silently stop every station from decoding, with no error anywhere.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "config" / "timestd-config.toml.template"


def _wwvb_section(text: str) -> str:
    m = re.search(r"^\[wwvb\]\s*$(.*?)(?=^\[)", text, re.S | re.M)
    assert m, "[wwvb] section not found in the template"
    return m.group(1)


class WwvbTemplateWidthTests(unittest.TestCase):
    def setUp(self):
        self.section = _wwvb_section(TEMPLATE.read_text())

    def test_template_ships_a_narrow_channel(self):
        m = re.search(r"^sample_rate\s*=\s*(\d+)", self.section, re.M)
        self.assertIsNotNone(m, "[wwvb] must declare sample_rate")
        self.assertEqual(int(m.group(1)), 4000)

    def test_channel_is_not_wide_enough_to_bury_the_signal(self):
        """Guards the class of mistake rather than the exact value: anything
        much above a few kHz throws away SNR a 1 bit/s code cannot spare."""
        m = re.search(r"^sample_rate\s*=\s*(\d+)", self.section, re.M)
        self.assertLessEqual(int(m.group(1)), 8000)

    def test_carrier_frequency_unchanged(self):
        """Narrowing the channel must not have moved the tuning."""
        m = re.search(r"^frequency_hz\s*=\s*(\d+)", self.section, re.M)
        self.assertEqual(int(m.group(1)), 60000)


if __name__ == "__main__":
    unittest.main()
