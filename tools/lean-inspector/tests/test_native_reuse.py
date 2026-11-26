"""Normal inspect entry remains authoritative when a lightweight seed is available."""
from test_native_support import *
from test_reuse import EXECUTION


class NativeLocalReportConsumerTests:
    def test_indexed_producer_repairs_evicted_compiler_shard_with_current_report(self):
        for name in ('lean_actions.py', 'lean_actions_shards.py', 'lean_artifact_inventory.py',
                     'lean_cache_shards.py', 'lean_release_index.py'):
            self.copy('tools/scripts/worktree/' + name)
        for name in ('ci-cache-paths.json', 'FILEMAP.toml'):
            self.copy('Meta/' + name)
        policy = json.loads((self.root / 'lean-report-inputs.json').read_text())
        policy['report_execution'] = EXECUTION
        policy['report_modules']['include'] = [
            dict(pattern='D5/**/*.lean', optional=False), dict(pattern='Reg/**/*.lean', optional=False)]
        self.write('lean-report-inputs.json', json.dumps(policy))
        self.write('utility.json', '[]')
        self.copy('tools/Makefile')
        self.copy('tools/lean-inspector/build_inputs.py')
        self.write('Reg/Required.lean', 'def regRequired : Nat := 1\n')
        self.write('tools/lean-inspector/LeanInformationAuditRegTests/Required.lean',
                   'def requiredCheck : Bool := true\n')
        dotnet = self.root / 'bin/dotnet'
        dotnet.write_text(dotnet.read_text().replace('if sys.argv[1] == "build":',
            'if sys.argv[1] == "restore": raise SystemExit(0)\nif sys.argv[1] == "build":'))
        self.write('.gitignore', '.lake/\nbuild/\n__pycache__/\nfixture-mathlib/\n')
        self.env.update(GITHUB_ACTIONS='true', GITHUB_EVENT_NAME='push',
            GITHUB_REF='refs/heads/integration-ci-cache-tests', GITHUB_REF_PROTECTED='true',
            GITHUB_RUN_ID='12', GITHUB_RUN_ATTEMPT='1', STRATALINT_CACHE_WRITES='true',
            STRATALINT_REPORT_SUCCEEDED='true', STRATALINT_CACHE_PRODUCER_COMPLETE='true',
            STRATALINT_ACCEPT_COLD_BUILD='1', STRATALINT_LEAN_BUILD_TARGETS='["leanInspector/reportInspector"]')

        def run(*args, **env):
            return self.guarded_command(args, env=dict(self.env, **env), timeout=120)

        for args in (('add', '.'), ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                   '-c', 'commit.gpgsign=false', 'commit', '-qm', 'native shard fixture')):
            git = run('git', *args)
            self.assertEqual(0, git.returncode, git.stdout + git.stderr)
        anchor = run('git', 'rev-parse', 'HEAD').stdout.strip()
        self.restore_compiler()
        checked = ('make', 'check-lean-report', 'FULL_BUILD=1', 'LEAN_REPORT_CACHE_MISS_POLICY=reuse-or-build')
        full = run(*checked, PARTIAL_INDEXED_PROJECT='false')
        self.assertEqual(0, full.returncode, full.stdout + full.stderr)

        shards = (sys.executable, 'tools/scripts/worktree/lean_actions_shards.py')
        snapshot = run(*shards, 'prepare', '--anchor', anchor)
        self.assertEqual(0, snapshot.returncode, snapshot.stdout + snapshot.stderr)
        index = self.root / 'project-index.json'
        index.write_text(snapshot.stdout.splitlines()[-1] + '\n')
        record = json.loads(index.read_text())
        key = next(key for key, group in record['layout'].items() if any('D5.Alone' in shard for shard in group))
        evicted = record['entries'][key]
        (self.root / 'build/lean-cache/shards/payload' / (evicted + '.tar.gz')).unlink()
        shutil.rmtree(self.root / '.lake/build')
        restored = run(*shards, 'install', '--index', str(index))
        self.assertEqual(0, restored.returncode, restored.stdout + restored.stderr)
        installed = json.loads(restored.stdout.splitlines()[-1])
        self.assertFalse(installed['complete'])
        self.assertEqual([evicted], installed['missing'])
        self.assertGreater(installed['available'], 0)
        build = self.root / '.lake/build'
        compiler = build / 'lib/lean/D5/Alone.olean'
        retained = build / 'reg/lib/lean/Reg/Required.olean'
        retained_mtime = retained.stat().st_mtime_ns
        receipt = build / 'stratalint/raw-lean-report.json.reuse.json'
        self.assertTrue(receipt.is_file())
        self.assertFalse(compiler.exists())

        raw_report = build / 'stratalint/raw-lean-report.json'
        publication.validate_bundle(raw_report, repository=self.root)
        prior_report = publication.digest(raw_report)
        rejected = run(*shards, 'prepare', '--anchor', anchor)
        self.assertIn('project snapshot is missing compiler or report outputs', rejected.stderr)
        self.assertNotEqual(0, rejected.returncode)

        repaired = run(*checked, PARTIAL_INDEXED_PROJECT='true')
        self.assertEqual(0, repaired.returncode, repaired.stdout + repaired.stderr)
        self.assertEqual(prior_report, publication.digest(raw_report))
        self.assertEqual(json.loads(receipt.read_text())['completed'][-1], 'full-report-checked')
        self.assertTrue(compiler.is_file())
        self.assertEqual(retained_mtime, retained.stat().st_mtime_ns)
        self.write('available-shards.json', json.dumps(sorted(set(record['entries'].values()) - {evicted})))
        published = run(*shards, 'prepare', '--index', str(index), '--available', 'available-shards.json')
        self.assertEqual(0, published.returncode, published.stdout + published.stderr)
        self.assertIn('"complete": true', published.stdout)
        index.write_text(published.stdout.splitlines()[-1] + '\n')
        shutil.rmtree(build)
        complete = run(*shards, 'install', '--index', str(index), '--require-complete')
        self.assertEqual(0, complete.returncode, complete.stdout + complete.stderr)
        self.assertTrue(json.loads(complete.stdout.splitlines()[-1])['complete'])
        self.assertTrue(compiler.is_file())
        self.assertTrue(receipt.is_file())

    def test_canonical_reader_runs_executable_git_adapter(self):
        result = self.guarded_command(['bash', 'tools/scripts/worktree/lean-cache-run.sh',
            'bash', '-c', 'command -v git && git --version'],
            env=dict(self.env, STRATALINT_ACCEPT_COLD_BUILD='1'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str((self.root / 'tools/scripts/worktree/lean-git/git').resolve()), result.stdout)
        self.assertIn('git version ', result.stdout)

class NativeReportConsumerTests:
    def test_shard_materializes_only_member_reports_and_reuses_them(self):
        self.reg_package()
        self.restore_compiler()
        worktree = HERE.parent / 'scripts/worktree'
        with patch.object(sys, 'path', [str(worktree), *sys.path]):
            from lean_artifact_inventory import Module, PROJECT_BUILD
            from lean_generation_facets import utility_inputs
            from lean_module_shards import pack_shard, report_paths, restore_shard
        executed = ('from lean_generation_facets import materialize; '
                    'from pathlib import Path; '
                    'materialize(Path.cwd(), ["Fixture", "D5.Alone"], ["External"])')
        environment = dict(self.env, PYTHONPATH=str(worktree), STRATALINT_ACCEPT_COLD_BUILD='1')
        programs = self.root / '.lake/build/lean-inspector/producer/bin'
        self.assertFalse((programs / 'reportInspector').exists())
        def materialize():
            self.write('activity.jsonl', '')
            result = self.guarded_command([sys.executable, '-B', '-c', executed], cwd=self.root, env=environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(set(self.stamps()), {'Fixture', 'D5.Alone'})
            return [json.loads(line) for line in (self.root / 'activity.jsonl').read_text().splitlines()]

        self.assertEqual(materialize(), [{'kind': 'extract', 'count': 2}])
        self.assertTrue((programs / 'reportInspector').is_file())
        self.assertTrue((programs / 'inputDiscovery').is_file())
        self.assertTrue((self.root / '.lake/build/lib/lean/External.olean').is_file())
        self.copy('src/OmegaChecks/Portable/ArtifactHashes.lean')
        build = self.root / PROJECT_BUILD
        names = ('Fixture', 'D5.Alone')
        with patch.dict(os.environ, environment):
            utilities = utility_inputs(self.root, names)
        modules = {}
        for name in names:
            source = Path(*name.split('.')).with_suffix('.lean')
            modules[name] = Module('trureturing', source, Path('.'), report=True,
                                   report_utility=utilities.get(source.as_posix()))
        artifacts = [build / path for name in modules for path in report_paths(name)]
        archive = self.root / 'module-shard.tar.gz'
        with patch.dict(os.environ, environment):
            descriptor = pack_shard(self.root, modules, sorted(modules), archive, 'a' * 64)
            for path in artifacts:
                path.unlink()
            self.assertEqual(restore_shard(archive, descriptor, modules, modules, build, root=self.root), set(modules))
        stamps = [path.stat().st_mtime_ns for path in artifacts]
        environment.update(LAKE_ARTIFACT_CACHE='false', LAKE_RESTORE_ARTIFACTS='false')
        self.assertEqual(materialize(), [])
        self.assertEqual([path.stat().st_mtime_ns for path in artifacts], stamps)

    def test_reg_dependency_closure_reassessment(self):
        # Typed Reg leaf and interface inputs select their compiler dependency closure.
        # Tiny typed transport inputs exercise production discovery, facets and publication;
        # mathematical assessment belongs to the compiled Reg judge tests.
        self.reg_package()
        core = self.root / 'tools/lean-inspector-interface/LeanInformationAuditInterface/Contract/Core.lean'
        original = core.read_text()
        contract = '\nnamespace LeanInformationAudit.Contract\nstructure Registration where\n  value : Nat\nend LeanInformationAudit.Contract\n'
        core.write_text(original + contract)
        policy = json.loads((self.root / 'lean-report-inputs.json').read_text())
        policy['config_inputs']['include'].append(dict(
            pattern='tools/lean-inspector-interface/**/*.lean', optional=False))
        self.write('lean-report-inputs.json', json.dumps(policy))
        self.write('Reg/Leaf.lean', 'import LeanInformationAuditInterface.Contract.Core\n'
            'def Reg.Leaf.entry : LeanInformationAudit.Contract.Registration := { value := 1 }\n')
        self.write('Reg/Parent.lean', 'import Reg.Leaf\n'
            'def Reg.Parent.entry : LeanInformationAudit.Contract.Registration := { value := Reg.Leaf.entry.value + 1 }\n')
        self.write('Reg/Unrelated.lean', 'def Reg.Unrelated.value : Nat := 1\n')
        support = 'LeanInformationAudit/Support.lean'
        self.write(support, 'module\npublic theorem registryPin : True := True.intro\n')
        driver = self.root / 'LeanInformationAudit/TemplateEnrollment.lean'
        driver.write_text('module\npublic import LeanInformationAudit.Support\n'
                          'public def fixtureDriver : Nat := 1\n')
        work = self.root / 'module-work.jsonl'
        self.env['STRATALINT_INSPECTOR_MODULE_WORK'] = str(work)
        output = self.root / 'public.json'

        def entry(phase, *, env=None):
            work.write_text('')
            before = self.stamps()
            oleans = {path: (path.stat().st_mtime_ns, publication.digest(path))
                      for path in (self.root / '.lake/build/reg').rglob('*.olean')}
            self.prepare_report_modules(workspace='tools/lean-inspector-reg')
            result = self.guarded_command([self.lake, '-d', 'tools/lean-inspector-reg', 'build', ':moduleReports'],
                env=dict(self.env if env is None else env, TEST_REPORT_REGISTRATION_GATE='1'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            records = [json.loads(line) for line in work.read_text().splitlines()]
            data = dict(exit_code=result.returncode, modules=len(self.stamps()),
                extracted=sorted(row['module'] for row in records if row['operation'] == 'extract'),
                assessed=sorted(row['module'] for row in records if row['operation'] == 'assess'),
                reg_recompiled=sorted(path.relative_to(self.root).as_posix() for path in oleans
                    if (path.stat().st_mtime_ns, publication.digest(path)) != oleans[path]),
                report_changed=sorted(name for name, stamp in self.stamps().items()
                    if name not in before or stamp != before[name]),
                warm=not records)
            self.record_result(phase, data, [work])
            return data

        # Hold the shared prerequisite inside Lake's lookup/create gap until
        # an independent consumer contends on graphFetch or creates it again.
        # Observe a pending predecessor after the real atomic queue append;
        # never infer overlap from time or replace the queue algorithm.
        config = self.root / 'lakefile.toml'
        config.write_text(config.read_text().replace(
            'name = "LeanInformationAudit"\n',
            'name = "LeanInformationAudit"\nneeds = ["leanInspector/compilerRegistrationGate"]\n'))
        producer_config = self.root / 'tools/lean-inspector/lakefile.lean'
        source = producer_config.read_text()
        schedule = '''
structure RegistrationSchedule where
  state : Std.Mutex (Nat × Nat)
  firstRegistration : IO.Promise Unit
  release : IO.Promise String

package_facet compilerRegistrationState (_pkg : Package) : RegistrationSchedule := do
  Job.async do return ⟨← Std.Mutex.new (0, 0), ← IO.Promise.new, ← IO.Promise.new⟩

'''
        queued = '''  unless ← IO.hasFinished previous do
    let schedule ← liftM (m := FetchM) do
      (← fetch <| (← getWorkspace).root.facet `compilerRegistrationState).await
    liftM (m := BaseIO) <| schedule.state.atomically fun state => do
      let (registered, waiting) ← state.get
      state.set (registered, waiting + 1)
      if registered > 0 then schedule.release.resolve "contended"
'''
        # Only the test observer needs FetchM. All production call sites already
        # run in FetchM or JobM; their critical sections remain unchanged.
        mutex = 'private def CompilerMutex.atomically [Monad m] [MonadLiftT BaseIO m] [MonadFinally m]\n'
        wait = '  discard <| IO.wait previous\n'
        self.assertEqual(source.count(mutex), 1)
        self.assertEqual(source.count(wait), 1)
        source = source.replace(mutex, schedule + mutex.replace(
            '[MonadFinally m]', '[MonadLiftT FetchM m] [MonadFinally m]')).replace(
            wait, queued + wait)
        contender = '''
package_facet compilerRegistrationContender (pkg : Package) : Unit := do
  let schedule ← (← fetch <| (← getWorkspace).root.facet `compilerRegistrationState).await
  let reportState ← (← fetch <| pkg.facet `reportBatch).await
  let some registry := (← getWorkspace).findModule? `LeanInformationAudit.TemplateEnrollment
    | error "missing fixture enrollment module"
  Job.async do
    discard <| IO.wait schedule.firstRegistration.result!
    let enrollment ← reportState.graphFetch.atomically fun _ =>
      JobM.runFetchM registry.exportInfo.fetch
    discard <| enrollment.await

'''
        if 'import Std.Sync.Mutex\n' not in source:
            source = 'import Std.Sync.Mutex\n' + source
        batch = 'package_facet reportBatch (_pkg : Package) : ReportState := do\n'
        inputs = 'package_facet reportInputs (pkg : Package) : FilePath := do\n'
        enrollment = '      let enrollment ← reportState.graphFetch.atomically fun _ =>\n'
        self.assertEqual(source.count(inputs), 1)
        self.assertEqual(source.count(enrollment), 1)
        producer_config.write_text(source.replace(
            batch, batch + '  discard <| (← fetch <| (← getWorkspace).root.facet `compilerRegistrationState).await\n').replace(
            inputs, contender + inputs).replace(enrollment,
                '      let contender ← JobM.runFetchM <| fetch <| pkg.facet `compilerRegistrationContender\n'
                '      deps := deps.add contender\n' + enrollment))
        with producer_config.open('a') as target:
            target.write('''\ntarget compilerRegistrationGate pkg : Unit := do
  unless (← IO.getEnv "TEST_REPORT_REGISTRATION_GATE") == some "1" do return Job.nil
  let root ← repositoryDir pkg
  let schedule ← (← fetch <| (← getWorkspace).root.facet `compilerRegistrationState).await
  schedule.state.atomically fun state => do
    let (registered, waiting) ← state.get
    state.set (registered + 1, waiting)
    IO.FS.withFile (root / "compiler-registrations") .append fun out => out.putStrLn "registered"
    if registered == 0 then schedule.firstRegistration.resolve ()
    if registered > 0 then schedule.release.resolve "duplicate"
    else if waiting > 0 then schedule.release.resolve "contended"
  let reason ← IO.wait schedule.release.result!
  IO.FS.writeFile (root / "compiler-registration-sync") reason
  return Job.nil
''')
        # Like the other direct-entry consumers, restore only the supplied
        # compiler material. Lake still builds every private project/report
        # output from cold and validates the modified contract traces.
        self.ensure()
        self.restore_compiler()
        self.check_cold_project(self)
        initial = entry('initial', env=dict(self.env, STRATALINT_ACCEPT_COLD_BUILD='1'))
        sync = self.root / 'compiler-registration-sync'
        registrations = self.root / 'compiler-registrations'
        self.record_result('compiler-registration', dict(
            registrations=registrations.read_text().splitlines(), release=sync.read_text()), [registrations, sync])
        self.assertEqual((self.root / 'compiler-registrations').read_text().splitlines(), ['registered'],
                         '[FAIL] shared compiler prerequisites must be registered once per Lake invocation')
        self.assertEqual(sync.read_text(), 'contended', '[FAIL] compiler registration overlap must be controlled')
        self.assertEqual(initial['assessed'], ['Reg.Leaf', 'Reg.Parent'])
        def report_hashes():
            return {name: json.loads((native.state(self.root) / 'modules' /
                    (name + '.zip.trace')).read_text())['depHash'] for name in self.stamps()}

        typed = ['Reg.Leaf', 'Reg.Parent']
        before = report_hashes()
        for phase, path, contents in [
            ('registry', driver, driver.read_text().replace(':= 1', ':= 2')),
            ('registry-import', self.root / support,
             'module\npublic theorem registryPin : True := Classical.choice ⟨True.intro⟩\n'),
        ]:
            path.write_text(contents)
            changed = entry(phase)
            self.assertEqual(changed['extracted'], typed)
            self.assertEqual(changed['assessed'], typed)
            self.assertEqual(changed['reg_recompiled'], [])
            after = report_hashes()
            self.assertEqual(sorted(name for name in after if before[name] != after[name]), typed)
            before = after
        warm = entry('warm-registry')
        self.assertEqual(warm['extracted'], [])
        self.assertEqual(warm['assessed'], [])
        self.assertEqual(warm['reg_recompiled'], [])
        self.assertTrue(warm['warm'])
        leaf = self.root / 'Reg/Leaf.lean'
        leaf.write_text(leaf.read_text().replace('value := 1', 'value := 2'))
        changed = entry('leaf')
        self.assertEqual(changed['extracted'], ['Reg.Leaf', 'Reg.Parent'])
        self.assertEqual(changed['assessed'], ['Reg.Leaf', 'Reg.Parent'])
        core.write_text(original + contract.replace('value : Nat', 'value : Int'))
        changed = entry('interface')
        self.assertEqual(changed['extracted'], ['Reg.Leaf', 'Reg.Parent'])
        self.assertEqual(changed['assessed'], ['Reg.Leaf', 'Reg.Parent'])
        self.assertEqual(len(changed['reg_recompiled']), 2)

    def test_impl_resource_preserves_production_reg_on_warm_report(self):
        # Use the default program targets of a direct report call, the package
        # target declarations, report entry and Lake compiler. Only the
        # mathematical inputs are tiny.
        implementations = ('CompiledAssessment', 'ArtifactAssessment')
        targets = ['leanInspector/LeanInformationAudit', 'leanInspector/reportInspector',
                   'leanInspectorInterface/LeanInformationAuditInterface', 'reg/Reg',
                   'regInspector/LeanInformationAuditRegTests']
        self.reg_package()
        # Restore the same compiler materials without producing an unrelated
        # report before this case has installed its production configuration.
        self.ensure()
        self.restore_compiler()
        self.check_cold_project(self)
        root_config = self.root / 'lakefile.toml'
        source = root_config.read_text()
        start = source.index('[[lean_lib]]\nname = "LeanInformationAudit"\n')
        stop = source.find('[[', start + 2)
        root_config.write_text(source[:start] + (source[stop:] if stop != -1 else ''))
        registry = 'LeanInformationAudit/TemplateEnrollment.lean'
        registry_owner = 'tools/lean-inspector/' + registry
        for module in [registry]:
            owner = self.root / 'tools/lean-inspector' / module
            owner.parent.mkdir(parents=True, exist_ok=True)
            (self.root / module).rename(owner)
        inspector_config = self.root / 'tools/lean-inspector/lakefile.lean'
        source = inspector_config.read_text()
        start = source.index('lean_lib LeanInformationAudit where\n')
        stop = source.index('lean_exe reportInspector where', start)
        inspector_config.write_text(source[:start] + 'lean_lib LeanInformationAudit where\n'
            '  globs := #[.submodules `LeanInformationAudit]\n\n' + source[stop:])
        def implementation(name, body):
            return (f'namespace LeanInformationAudit.{name}\n'
                    f'def fixtureValue (input : Nat) : Nat := {body}\n'
                    f'end LeanInformationAudit.{name}\n')
        for name in implementations:
            self.write(f'tools/lean-inspector/LeanInformationAudit/{name}.lean',
                       implementation(name, 'input + 1'))
        # CompiledAssessment is an actual import of the fixture's report driver, so
        # its body edit exercises the TemplateEnrollment closure as well as defaults.
        driver = self.root / registry_owner
        driver.write_text('import LeanInformationAudit.ArtifactAssessment\n'
                          'import LeanInformationAudit.CompiledAssessment\n' + driver.read_text())
        self.write('Reg/ProductionOnly.lean', 'import LeanInformationAuditInterface.Contract.Core\n'
                   'import D5.A\ndef productionValue : Nat := value\n')
        # Required.lean deliberately imports no production Reg module.
        policy = json.loads((self.root / 'lean-report-inputs.json').read_text())
        policy['report_execution'] = EXECUTION
        for row in policy['dependency_sources']['include']:
            if row['pattern'] == registry:
                row['pattern'] = registry_owner
        for name in implementations:
            policy['dependency_sources']['include'].append(dict(
                pattern=f'tools/lean-inspector/LeanInformationAudit/{name}.lean', optional=False))
        self.write('lean-report-inputs.json', json.dumps(policy))
        self.env['STRATALINT_LEAN_BUILD_TARGETS'] = json.dumps(targets)
        output = self.root / 'public.json'

        def entry(phase, *, env=None):
            result = self.guarded_command(['make', 'lean-cache-prepare'],
                env=self.env if env is None else env)
            self.record_result(phase, dict(exit_code=result.returncode,
                stdout=result.stdout, stderr=result.stderr, targets=targets))
            return result

        self.assertFalse(output.exists(), '[FAIL] configured_report_must_start_cold')
        self.assertEqual(self.stamps(), {}, '[FAIL] no_report_before_configured_production')
        self.prepare_report_modules(workspace='tools/lean-inspector-reg')
        initial = entry('production-initial', env=dict(self.env, STRATALINT_ACCEPT_COLD_BUILD='1'))
        self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
        expected = self.report()[1:]
        report_stamps = self.stamps()
        untouched = [* (self.root / '.lake/build/lib/lean/D5').rglob('*.olean'),
                     * (self.root / '.lake/build/reg/lib/lean/Reg').rglob('*.olean'),
                     * (self.root / '.lake/build/lean-inspector/interface').rglob('*.olean'),
                     * (self.root / '.lake/packages/mathlib/.lake/build').rglob('*.olean')]
        self.assertTrue(untouched)
        self.assertTrue(any('interface' in path.parts for path in untouched))
        stamps = {path: (path.stat().st_mtime_ns, publication.digest(path)) for path in untouched}
        production = self.root / '.lake/build/reg/lib/lean/Reg/ProductionOnly.olean'
        self.assertIn(production, stamps)
        for name in implementations:
            with self.subTest(implementation=name):
                self.write(f'tools/lean-inspector/LeanInformationAudit/{name}.lean',
                           implementation(name, 'input + 2'))
                compiled = entry(f'production-warm-{name}')
                self.assertNotIn('phase=programs ', compiled.stderr)
                self.assertEqual(stamps, {path: (path.stat().st_mtime_ns, publication.digest(path))
                                          for path in untouched})
                self.assertEqual(report_stamps, self.stamps())
                self.assertEqual(expected, self.report()[1:])

        self.write('tools/lean-inspector/LeanInformationAudit/ArtifactAssessment.lean',
                   implementation('ArtifactAssessment', 'true'))
        rejected = entry('invalid-program')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('ArtifactAssessment.lean:', rejected.stderr)
        self.assertFalse((self.root / 'build/lean-cache/build-work.json').exists())
        self.assertEqual(expected, self.report()[1:])
        self.assertEqual(report_stamps, self.stamps())
