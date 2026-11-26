"""Exercise the published report consumer against the source-pinned module ZIP format."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import report

TOOLS = Path(__file__).resolve().parents[3] / 'tools'
sys.path.insert(0, str(TOOLS / 'lean-inspector'))
import materials
import native
import publication


class ReleaseReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = native.state(self.root) / 'modules'
        self.artifacts.mkdir(parents=True)
        self.selection = self.enterContext(patch.object(publication.selection, 'Selection')).return_value
        self.enterContext(patch.dict(os.environ, TRUTH_RELEASE_BASE=''))

    def write_zip(self, name, text):
        row = dict(module=name, imports=[], declarations=[], source_path=name + '.lean',
                   source_sha256='sha256:' + 'a' * 64,
                   information_templates={'module': 'nested', 'text': text})
        module = dict(modules=[row], schema=materials.REPORT_SCHEMA)
        origin = dict(module=name, input_projection=dict(module=name, inputs=[],
                      schema='stratalint-judge-input-projection-v1'), report_sha256='0' * 64,
                      producer_sources_sha256='0' * 64, inspector_executable_sha256='0' * 64)
        spool = io.BytesIO()
        with zipfile.ZipFile(spool, 'w'):
            pass
        with zipfile.ZipFile(self.artifacts / (name + '.zip'), 'w') as zip_file:
            zip_file.writestr(publication.RAW, materials.canonical_json(module))
            zip_file.writestr(publication.RAW + '.materials.zip', spool.getvalue())
            zip_file.writestr(publication.RAW + '.provenance.json', materials.canonical_json(origin))
        return row

    def test_membership_merge_matches_full_and_opens_only_changed_zips(self):
        name_a = 'A.角'
        rows = {name: self.write_zip(name, 'quote: "}, {"declarations": [\\😀') for name in
                (name_a, 'B.Removed', 'C.Changed', 'Z.Reused')}
        self.selection.modules.return_value = rows
        previous = self.root / 'raw-lean-report.json'
        report.assemble(self.root, previous)
        # Compare the cold output to the pinned Inspector's original module aggregator.
        expected_rows = []
        for name in sorted(rows):
            encoded = io.BytesIO()
            native.aggregate_module(name, self.artifacts / (name + '.zip'), encoded, io.BytesIO(), {})
            expected_rows.append(encoded.getvalue())
        expected = report.PREFIX + b', '.join(expected_rows) + b'], "schema": "stratalint-raw-lean-report-v4"}\n'
        self.assertEqual(previous.read_bytes(), expected)
        self.assertFalse(Path(str(previous) + '.materials.zip').exists())

        del rows['B.Removed']
        rows['AA.New'] = self.write_zip('AA.New', 'insert')
        rows['C.Changed'] = self.write_zip('C.Changed', 'replace')
        full = self.root / 'full.json'
        report.assemble(self.root, full)
        reused = [name_a, 'Z.Reused']
        for name in [*reused, 'B.Removed']:
            (self.artifacts / (name + '.zip')).unlink()
        (self.root / 'reuse.json').write_text(json.dumps(reused))
        os.environ['TRUTH_RELEASE_BASE'] = str(self.root)
        incremental = self.root / 'incremental.json'
        with patch.object(report, 'write_changed_row', wraps=report.write_changed_row) as write:
            report.assemble(self.root, incremental)
            self.assertEqual([call.args[1] for call in write.call_args_list], ['AA.New', 'C.Changed'])
        self.assertEqual(incremental.read_bytes(), full.read_bytes())

    def test_missing_reused_row_and_incompatible_schema(self):
        self.selection.modules.return_value = ['Present']
        self.write_zip('Present', 'one')
        previous = self.root / 'raw-lean-report.json'
        report.assemble(self.root, previous)
        os.environ['TRUTH_RELEASE_BASE'] = str(self.root)
        (self.root / 'reuse.json').write_text('["Absent"]')
        self.selection.modules.return_value = ['Absent']
        with self.assertRaisesRegex(ValueError, 'lacks the requested'):
            report.assemble(self.root, self.root / 'missing.json')
        self.selection.modules.return_value = ['Present']
        (self.root / 'reuse.json').write_text('["Present"]')
        previous.write_bytes(previous.read_bytes().replace(b'raw-lean-report-v4', b'raw-lean-report-v3'))
        with self.assertRaisesRegex(ValueError, 'incompatible predecessor'):
            report.assemble(self.root, self.root / 'incompatible.json')
        (self.root / 'reuse.json').write_text('[]')
        report.assemble(self.root, self.root / 'cold.json')


if __name__ == '__main__':
    unittest.main()
