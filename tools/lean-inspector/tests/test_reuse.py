"""Synthetic inputs and execution registration tests for report consumers."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import materials
import publication

EXECUTION = dict(toolchain='lean-toolchain', tools=['lake', 'lean'], platform=['system', 'machine'],
    environment=['LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT', 'ELAN_TOOLCHAIN', 'LEAN_OPTS'])


class ReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='report entry reuse ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        paths = lambda *values: dict(include=[dict(pattern=v, optional=False) for v in values], exclude=[])
        self.policy = dict(schema_version=1,
            report_modules=paths('D5/**/*.lean'), inspector_sources=paths('Inspector.lean'),
            dependency_sources=paths('Audit.lean'), config_inputs=paths('lean-toolchain', 'lakefile.toml'),
            producer_scopes={'lean-report': paths('lean-report-inputs.json',
                'tools/scripts/report/lean-report-selection.py', 'producer.py'),
                'scribe-content': dict(include=[], exclude=[])}, report_execution=copy.deepcopy(EXECUTION))
        for path, value in {'D5/A.lean': 'def a := 1\n', 'Audit.lean': 'def audit := 1\n',
                'Inspector.lean': 'def inspector := 1\n', 'producer.py': '# producer\n',
                'lean-toolchain': 'fixture\n', 'lakefile.toml': 'name = "fixture"\n',
                'lake-manifest.json': json.dumps({'packages': [{'name': 'mathlib', 'rev': 'a' * 40}]})}.items():
            self.write(path, value)
        self.write_policy()
        for name in ['tools/scripts/report/lean-report-selection.py', 'tools/scripts/report/lean-report-input.sh',
                'tools/scripts/worktree/lean-cache-input.sh']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
            path.chmod(0o755)
        self.lake = self.root / 'bin/lake'
        for name in EXECUTION['tools']:
            path = self.root / 'bin' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('#!/bin/sh\n[ "$1" = --version ] || exit 91\nprintf "%s\\n" "fixture ' + name + ' 1"\n')
            path.chmod(0o755)
        self.report = self.root / 'seed' / publication.RAW
        self.report.parent.mkdir()
        self.output = self.root / 'output' / publication.RAW
        self.environment = patch.dict(os.environ, {name: '' for name in EXECUTION['environment']})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)

    def write_policy(self):
        self.write('lean-report-inputs.json', json.dumps(self.policy))

    def bundle(self):
        inputs = publication.selection.Selection(self.root)
        rows = [dict(module=name, source_path=path,
                     source_sha256='sha256:' + publication.digest(inputs.safe_file(path)),
                     imports=[], declarations=[]) for name, path in sorted(inputs.modules().items())]
        self.report.write_bytes(materials.canonical_json(dict(schema=materials.REPORT_SCHEMA, modules=rows)))
        with zipfile.ZipFile(publication.member(self.report, '.materials.zip'), 'w'):
            pass
        origins = {row['module']: dict(module=row['module'],
            report_sha256=hashlib.sha256(materials.canonical_json(
                dict(schema=materials.REPORT_SCHEMA, modules=[row]))).hexdigest(),

            input_projection=dict(schema='stratalint-judge-input-projection-v1', module=row['module'], inputs=[]), producer_sources_sha256='a' * 64,
            inspector_executable_sha256='b' * 64) for row in rows}
        publication.write_sidecars(self.report, publication.coordinates(self.root), origins)

    def test_execution_registration_is_explicit_and_strict(self):
        try:
            publication.selection.Selection(self.root).validate('lean-report')
        except ValueError as error:
            self.fail('[FAIL] registered_execution_rejected: ' + str(error))
        original = copy.deepcopy(self.policy)
        for bad in [None, {}, dict(EXECUTION, tools=['lake', 'shell']),
                    dict(EXECUTION, tools=['lake']), dict(EXECUTION, platform=['system', 'system']),
                    dict(EXECUTION, environment=['PATH']), dict(EXECUTION, commands=['anything'])]:
            with self.subTest(contract=bad):
                self.policy['report_execution'] = bad
                self.write_policy()
                with self.assertRaisesRegex(ValueError, 'report_execution'):
                    publication.selection.Selection(self.root)
        self.policy = original
        del self.policy['report_execution']
        self.write_policy()
        publication.selection.Selection(self.root).validate('lean-report')
