"""hf-timestd has no uploader; `[uploader]` is a retired section.

hs-uploader is a station's single path to PSWS (since 2026-07-15), and GRAPE
packaging left for hamsci-physics in the 2026-08-24 split.  Nothing in
hf-timestd reads `[uploader]` and uploads, yet the template shipped the block
with `enabled = true` and setup-station.sh wrote `enabled = true` plus a key
path (`id_rsa_psws_<ID>`) that nothing creates.  AC0G-B4's v3.65 install
carried exactly that, 2026-10-01: a switch wired to nothing.

So: the template and the wizard stop writing the block, and `validate` warns
on any deployed config that still carries it.  `config apply` keeps ACCEPTING
the section, because every deployed config holds it and sigmond's editor
round-trips the whole file; rejecting it would break config edits fleet-wide.
"""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from hf_timestd.cli import retired_section_issues


class UploaderSectionWarnsTests(unittest.TestCase):

    def test_no_section_raises_no_issue(self):
        self.assertEqual(
            [i for i in retired_section_issues({}) if '[uploader]' in i['message']], [])

    def test_deployed_block_warns_and_names_hs_uploader(self):
        cfg = {'uploader': {'enabled': True, 'protocol': 'sftp',
                            'sftp': {'host': 'pswsnetwork.eng.ua.edu'}}}
        issues = [i for i in retired_section_issues(cfg) if '[uploader]' in i['message']]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['severity'], 'warn')
        self.assertIn('hs-uploader', issues[0]['message'])
        self.assertIn('remove the section', issues[0]['message'])

    def test_disabled_block_warns_the_same_way(self):
        # `enabled = false` is no less dead than true: neither switches anything.
        issues = [i for i in retired_section_issues({'uploader': {'enabled': False}})
                  if '[uploader]' in i['message']]
        self.assertEqual(len(issues), 1)


class NothingShipsOrWritesTheBlockTests(unittest.TestCase):

    def test_template_has_no_uploader_section(self):
        with open(ROOT / 'config' / 'timestd-config.toml.template', 'rb') as f:
            cfg = tomllib.load(f)
        self.assertNotIn('uploader', cfg)

    def test_help_has_no_uploader_entries(self):
        with open(ROOT / 'config' / 'help.toml', 'rb') as f:
            hlp = tomllib.load(f)
        self.assertNotIn('uploader', hlp)

    def test_setup_station_writes_no_uploader_keys(self):
        text = (ROOT / 'scripts' / 'setup-station.sh').read_text()
        for needle in ('"uploader"', '"uploader.sftp"', 'UPLOADER_ENABLED',
                       'id_rsa_psws_', 'setup-psws-keys.sh'):
            self.assertNotIn(needle, text, f'setup-station.sh still carries {needle}')

    def test_config_apply_still_accepts_a_deployed_block(self):
        from hf_timestd.configurator import _APPLY_ALLOWED_SECTIONS
        self.assertIn('uploader', _APPLY_ALLOWED_SECTIONS)


if __name__ == '__main__':
    unittest.main()
