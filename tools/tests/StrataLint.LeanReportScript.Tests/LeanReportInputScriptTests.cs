using StrataLint.Runtime;
using System.Security.Cryptography;
using System.Text;
using StrataLint.Engine;

namespace StrataLint.LeanReportScript.Tests;

[Collection("Lean report environment")]
public sealed partial class LeanReportInputScriptTests
{
    private const string InputHelperPath = "tools/scripts/report/lean-report-input.sh";
    private const string RawReportPath = "tools/StrataLint.Engine/Snapshot/RawLeanReportArtifact.cs";
    private const string CanonicalWriterPath = "tools/Trureturing.Truth/StructuredCanonicalWriter.cs";
    private const string LeanModelsPath = "tools/StrataLint.Engine/Snapshot/LeanModels.cs";
    private const string TestSourcePath = "tools/tests/StrataLint.Tests/Snapshot/LeanModelsTests.cs";
    private const string BlueprintSourcePath = "Blueprint/D5/Probe.scribe.cs";
    private const string ScribeSourcePath = "tools/StrataLint.Scribe/Emission/FixtureEmitter.cs";
    private const string ScribeContentChecksPath =
        "tools/scripts/workflow/scribe-content-checks.sh";
    private static readonly string PairScriptPath = string.Join(
        '/', "tools", "scripts", "lean-report-pair.sh");
    private const string SupervisorScriptPath = "tools/scripts/report/report-supervisor.sh";
    private const string NativeProducerPath = "tools/lean-inspector/native.py";
    private const string CacheEnsureScriptPath =
        "tools/scripts/worktree/lean-cache-ensure.sh";
    private const string CachePublishScriptPath =
        "tools/scripts/worktree/lean-cache-publish.sh";
    private const string ResourceObservationLibraryPath =
        "tools/scripts/lib/resource-observation-lib.sh";
    private const string ToolchainInstallerPath = "tools/scripts/workflow/install-lean-toolchain.sh";
    private const string WorkflowPath = ".github/workflows/ci.yml";
    private static readonly string CliProjectPath = string.Join(
        '/', "tools", "StrataLint.Cli", "StrataLint.Cli.csproj");
    private static readonly string EngineProjectPath = string.Join(
        '/', "tools", "StrataLint.Engine", "StrataLint.Engine.csproj");
    private static readonly string ScribeProjectPath = string.Join(
        '/', "tools", "StrataLint.Scribe", "StrataLint.Scribe.csproj");
    private static readonly string EngineLockPath = string.Join(
        '/', "tools", "StrataLint.Engine", "packages.lock.json");
    private static readonly string CliLockPath = string.Join(
        '/', "tools", "StrataLint.Cli", "packages.lock.json");
    private static readonly string ScribeLockPath = string.Join(
        '/', "tools", "StrataLint.Scribe", "packages.lock.json");
    private static readonly string TruthProjectPath = string.Join(
        '/', "tools", "Trureturing.Truth", "Trureturing.Truth.csproj");
    private static readonly string TruthLockPath = string.Join(
        '/', "tools", "Trureturing.Truth", "packages.lock.json");

    [Fact]
    public void CompatibleProductionSourceEditPreservesSemanticCompatibility()
    {
        using var fixture = new LeanReportInputFixture();
        var producerBefore = fixture.Producer();

        fixture.Append(LeanModelsPath, "// mutation\n");

        Assert.Equal(producerBefore, fixture.Producer());
    }

    [Fact]
    public void TestProjectSourceDoesNotChangeProducer()
    {
        using var fixture = new LeanReportInputFixture();
        var before = fixture.Producer();

        fixture.Append(TestSourcePath, "// mutation\n");

        Assert.Equal(before, fixture.Producer());
    }

    [Fact]
    public void BlueprintScribeSourceDoesNotChangeProducer()
    {
        using var fixture = new LeanReportInputFixture();
        var before = fixture.Producer();

        fixture.Append(BlueprintSourcePath, "// mutation\n");

        Assert.Equal(before, fixture.Producer());
    }

    [Fact]
    public void CompatibleBuildPropsEditPreservesSemanticCompatibility()
    {
        using var fixture = new LeanReportInputFixture();
        var before = fixture.Producer();

        fixture.Append("Directory.Build.props", "<!-- mutation -->\n");

        Assert.Equal(before, fixture.Producer());
    }

    // Report inputs and their failure boundaries come from explicit registrations.
    [Fact]
    public void CompileClosureFailureCannotProduceCollidingValidAddresses()
    {
        using var left = new LeanReportInputFixture();
        using var right = new LeanReportInputFixture();
        left.BreakProducerClosureEvaluation();
        right.BreakProducerClosureEvaluation();
        right.Append(LeanModelsPath, "// the only repository-content difference\n");

        Assert.Equal(2, left.RunCommand("producer-paths").ExitCode);
        Assert.Equal(2, right.RunCommand("producer-paths").ExitCode);
        var leftResult = left.RunCommand("address");
        var rightResult = right.RunCommand("address");

        Assert.Equal(2, leftResult.ExitCode);
        Assert.Equal(2, rightResult.ExitCode);
        Assert.Empty(leftResult.StandardOutput);
        Assert.Empty(rightResult.StandardOutput);
    }

    [Fact]
    public void CompleteProducerClosureHasStableAddress()
    {
        using var fixture = new LeanReportInputFixture();

        var first = fixture.RunCommand("address");
        var second = fixture.RunCommand("address");

        Assert.Equal(0, first.ExitCode);
        Assert.Equal(0, second.ExitCode);
        Assert.Equal(first.StandardOutput, second.StandardOutput);
    }

    [Fact]
    public void ProducerPathsDeriveEveryReachableShellDependency()
    {
        using var fixture = new LeanReportInputFixture();
        const string derivedProbe = "tools/scripts/report/derived-producer.sh";
        fixture.WriteSource(derivedProbe, "#!/usr/bin/env bash\n");
        fixture.RegisterProducer(derivedProbe);
        fixture.Append(PairScriptPath, "\n\"$SCRIPT_DIR/report/derived-producer.sh\"\n");

        var result = fixture.RunCommand("producer-paths");

        Assert.True(
            result.ExitCode == 0,
            Encoding.UTF8.GetString(result.StandardError));
        var paths = Lines(result);
        Assert.Contains(InputHelperPath, paths);
        Assert.Contains("Directory.Build.props", paths);
        Assert.Contains(RawReportPath, paths);
        Assert.Contains(LeanModelsPath, paths);
        Assert.Contains(SupervisorScriptPath, paths);
        Assert.Contains(NativeProducerPath, paths);
        Assert.Contains(CacheEnsureScriptPath, paths);
        Assert.Contains(CachePublishScriptPath, paths);
        Assert.Contains(ResourceObservationLibraryPath, paths);
        Assert.Contains(ToolchainInstallerPath, paths);
        Assert.DoesNotContain("tools/scripts/workflow/judge-content-address.sh", paths);
        Assert.DoesNotContain(WorkflowPath, paths);
        Assert.Contains(derivedProbe, paths);
        Assert.DoesNotContain(TestSourcePath, paths);
        Assert.DoesNotContain(BlueprintSourcePath, paths);
    }

    [Fact]
    public void ProducerPathsCommandExposesTheCanonicalDeclaredAndCompileClosure()
    {
        using var fixture = new LeanReportInputFixture();

        var result = fixture.RunCommand("producer-paths");

        Assert.Equal(0, result.ExitCode);
        var paths = Lines(result);
        Assert.Contains(InputHelperPath, paths);
        Assert.Contains("Directory.Build.props", paths);
        Assert.Contains(RawReportPath, paths);
        Assert.Contains(LeanModelsPath, paths);
        Assert.Contains(CacheEnsureScriptPath, paths);
        Assert.Contains(ResourceObservationLibraryPath, paths);
        Assert.DoesNotContain("tools/scripts/workflow/judge-content-address.sh", paths);
        Assert.DoesNotContain(TestSourcePath, paths);
        Assert.DoesNotContain(BlueprintSourcePath, paths);
    }

    [Fact]
    public void ScribeProducerPathsDeriveCompileItemsAndReachableShellDependencies()
    {
        using var fixture = new LeanReportInputFixture();
        const string derivedProbe = "tools/scripts/workflow/derived-scribe-input.sh";
        fixture.WriteSource(derivedProbe, "#!/usr/bin/env bash\n");
        fixture.RegisterProducer(derivedProbe, "scribe-content");
        fixture.Append(
            ScribeContentChecksPath,
            "\n\"$REPO_ROOT/tools/scripts/workflow/derived-scribe-input.sh\"\n");

        var result = fixture.RunCommand("scribe-producer-paths");

        Assert.Equal(0, result.ExitCode);
        var paths = Lines(result);
        Assert.Contains(BlueprintSourcePath, paths);
        Assert.Contains(ScribeSourcePath, paths);
        Assert.Contains(LeanModelsPath, paths);
        Assert.Contains(ScribeProjectPath, paths);
        Assert.Contains(ScribeContentChecksPath, paths);
        Assert.DoesNotContain("tools/scripts/workflow/judge-content-address.sh", paths);
        Assert.Contains(CachePublishScriptPath, paths);
        Assert.Contains(derivedProbe, paths);
        Assert.DoesNotContain(TestSourcePath, paths);
    }

    [Fact]
    public void ModulesEnumerateAllManagedSources()
    {
        using var fixture = new LeanReportInputFixture();
        fixture.WriteSource("D5/Nested/Second.lean", "def second : Nat := 2\n");

        var modules = fixture.RunCommand("modules");

        Assert.Equal(0, modules.ExitCode);
        Assert.Equal(
            new[]
            {
                "Trureturing\tTrureturing.lean",
                "D5.Nested.Second\tD5/Nested/Second.lean",
                "D5.Probe\tD5/Probe.lean",
            },
            Lines(modules));
    }

    [Fact]
    public void CacheClosureHashesSeparateConfigurationFromSources()
    {
        using var fixture = new LeanReportInputFixture();
        if (!OperatingSystem.IsWindows()) fixture.InitializeGitRepository();
        var before = fixture.CacheIdentity();
        fixture.AssertMemoBehavior(before);

        fixture.Append("lean-toolchain", "mutation\n");
        var configChanged = fixture.CacheIdentity();
        Assert.NotEqual(before.Config, configChanged.Config);
        Assert.Equal(before.Sources, configChanged.Sources);

        fixture.Append("D5/Probe.lean", "mutation\n");
        var sourceChanged = fixture.CacheIdentity();
        Assert.Equal(configChanged.Config, sourceChanged.Config);
        Assert.NotEqual(configChanged.Sources, sourceChanged.Sources);
    }

    [Theory]
    [InlineData("repository", "tools/lean-inspector/LeanInformationAudit/Nested/ProofBuilder.lean")]
    [InlineData("repository[cache]", "tools/lean-inspector/LeanInformationAudit/Nested/ProofBuilder.lean")]
    [InlineData("repository", "tools/lean-inspector-interface/LeanInformationAuditInterface/Contract/Core.lean")]
    [InlineData("repository[cache]", "tools/lean-inspector-interface/LeanInformationAuditInterface/Contract/Core.lean")]
    public void CompatibleInspectorLibraryEditPreservesReportAddress(string repositoryName, string judgeSource)
    {
        using var fixture = new LeanReportInputFixture(repositoryName);
        const string unrelated = "tools/lean-inspector/LeanInformationAudit/README.md";
        fixture.WriteSource(judgeSource, "def judgeFixture : True := by trivial\n");
        fixture.WriteSource(unrelated, "fixture documentation\n");
        var producerBefore = fixture.Producer();
        var before = fixture.Address();

        fixture.Append(unrelated, "x");
        Assert.Equal(before, fixture.Address());

        fixture.Append(judgeSource, "x");
        Assert.Equal(producerBefore, fixture.Producer());
        Assert.Equal(before, fixture.Address());
    }

    [Fact]
    public void AbsentJudgeLibraryContributesNoSourcesAndAddressSucceeds()
    {
        using var fixture = new LeanReportInputFixture();
        fixture.RemoveSource("tools/lean-inspector/Inspector.lean");
        var result = fixture.RunCommand("address");
        Assert.Equal(0, result.ExitCode);
        Assert.Equal(fixture.ManifestHash("Trureturing.lean", "D5/Probe.lean"), Fields(result)[2]);
    }

    [Fact]
    public void AddingASecondSourceWithIdenticalContentsChangesTheSourcesHash()
    {
        using var fixture = new LeanReportInputFixture();
        if (!OperatingSystem.IsWindows()) fixture.InitializeGitRepository();
        var before = fixture.CacheIdentity();

        fixture.Append("D5/Probe.lean", " ");
        Assert.NotEqual(before.Sources, fixture.CacheIdentity().Sources);
        fixture.WriteSource("D5/Probe.lean", "theorem probe : True := by trivial\n");
        Assert.Equal(before, fixture.CacheIdentity());

        fixture.WriteSource("D5/Copy.lean", "theorem probe : True := by trivial\n");

        var after = fixture.CacheIdentity();
        Assert.Equal(before.Config, after.Config);
        Assert.NotEqual(before.Sources, after.Sources);
    }

    [Theory]
    [InlineData("source", true)]
    [InlineData("toolchain", true)]
    [InlineData("lakefile", true)]
    [InlineData("manifest", true)]
    [InlineData("inspector", false)]
    [InlineData("inspector-script", false)]
    [InlineData("input-helper", false)]
    [InlineData("raw-report", false)]
    [InlineData("canonical-writer", false)]
    [InlineData("cache-fetcher", false)]
    public void RepositoryInputDriftRespectsSemanticCompatibility(string mutation, bool invalidates)
    {
        using var fixture = new LeanReportInputFixture();

        var before = fixture.Address();
        fixture.Mutate(mutation);
        Assert.Equal(invalidates, before != fixture.Address());
    }

    private sealed partial class LeanReportInputFixture : IDisposable
    {
        private readonly TemporaryDirectory temporary = new();
        private readonly string repository;
        private readonly string script;
        private readonly string inspectorScriptPath = string.Join(
            '/', "tools", "lean-inspector", "inspect.sh");
        private readonly string inspectorSourcePath = string.Join(
            '/', "tools", "lean-inspector", "Inspector.lean");

        internal LeanReportInputFixture(string repositoryName = "repository")
        {
            repository = Path.Combine(temporary.Path, repositoryName);
            script = Path.Combine(
                TestRepositoryLayout.FindRoot(),
                "tools", "scripts", "report", "lean-report-input.sh");
            Directory.CreateDirectory(Path.Combine(repository, "D5"));
            Directory.CreateDirectory(Path.Combine(
                repository, "tools", "lean-inspector"));
            Write("Trureturing.lean", "import D5.Probe\n");
            Write("D5/Probe.lean", "theorem probe : True := by trivial\n");
            Write("lean-toolchain", "leanprover/lean4:v4.31.0\n");
            Write("lakefile.toml", "name = \"Fixture\"\n");
            Write("lake-manifest.json", "{\"version\":\"1.1.0\"}\n");
            Write(inspectorScriptPath, "#!/usr/bin/env bash\n");
            Write(inspectorSourcePath, "def fixture : True := by trivial\n");
            Write(InputHelperPath, "#!/usr/bin/env bash\n");
            Write("tools/scripts/worktree/lean-cache-input.sh", File.ReadAllText(
                Path.Combine(TestRepositoryLayout.FindRoot(), "tools/scripts/worktree/lean-cache-input.sh"),
                Encoding.UTF8));
            // Explicit fixture producer source registration.
            Write("tools/StrataLint.Cli/Commands/FixtureProbe.cs", "// fixture\n");
            Write(RawReportPath, "// fixture\n");
            Write(CanonicalWriterPath, "// fixture\n");
            Write(LeanModelsPath, "// fixture\n");
            Write(TestSourcePath, "// fixture\n");
            Write(BlueprintSourcePath, "// fixture\n");
            Write(ScribeSourcePath, "// fixture\n");
            Write(
                PairScriptPath,
                File.ReadAllText(
                    Path.Combine(
                        TestRepositoryLayout.FindRoot(),
                        "tools", "scripts", "lean-report-pair.sh"),
                    Encoding.UTF8));
            Write(
                SupervisorScriptPath,
                File.ReadAllText(
                    Path.Combine(
                        TestRepositoryLayout.FindRoot(),
                        "tools", "scripts", "report", "report-supervisor.sh"),
                    Encoding.UTF8));
            Write(
                NativeProducerPath,
                File.ReadAllText(
                    Path.Combine(
                        TestRepositoryLayout.FindRoot(),
                        "tools", "lean-inspector", "native.py"),
                    Encoding.UTF8));
            Write(CacheEnsureScriptPath, "#!/usr/bin/env bash\n");
            Write("tools/lean-inspector/publication.py", File.ReadAllText(
                Path.Combine(TestRepositoryLayout.FindRoot(), "tools", "lean-inspector", "publication.py")));
            Write("tools/lean-inspector/materials.py", File.ReadAllText(
                Path.Combine(TestRepositoryLayout.FindRoot(), "tools", "lean-inspector", "materials.py")));
            Write(
                CachePublishScriptPath,
                File.ReadAllText(
                    Path.Combine(
                        TestRepositoryLayout.FindRoot(),
                        "tools", "scripts", "worktree", "lean-cache-publish.sh"),
                    Encoding.UTF8));
            Write(
                ResourceObservationLibraryPath,
                File.ReadAllText(
                    Path.Combine(
                        TestRepositoryLayout.FindRoot(),
                        "tools", "scripts", "lib", "resource-observation-lib.sh"),
                    Encoding.UTF8));
            Write(ToolchainInstallerPath, "#!/usr/bin/env bash\n");
            Write(ScribeContentChecksPath, "#!/usr/bin/env bash\n");
            Write(WorkflowPath, "# unrelated fixture workflow\n");
            Write("Directory.Build.props", "<Project />\n");
            Write("Directory.Packages.props", "<Project />\n");
            Write(CliProjectPath, "<Project Sdk=\"Microsoft.NET.Sdk\" />\n");
            Write(EngineProjectPath, "<Project Sdk=\"Microsoft.NET.Sdk\" />\n");
            Write(ScribeProjectPath, "<Project Sdk=\"Microsoft.NET.Sdk\" />\n");
            Write(TruthProjectPath, "<Project Sdk=\"Microsoft.NET.Sdk\" />\n");
            Write(EngineLockPath, "{}\n");
            Write(CliLockPath, "{}\n");
            Write(ScribeLockPath, "{}\n");
            Write(TruthLockPath, "{}\n");
            Write("global.json", "{}\n");
            LeanReportRegistrationFixture.Install(repository);
        }

        internal void RegisterProducer(string path, string scope = "lean-report") =>
            LeanReportRegistrationFixture.RegisterProducer(repository, path, scope);

        internal string MemoRoot => Path.Combine(temporary.Path, "memo");

        internal string MemoFile => Path.Combine(MemoRoot, "memo.v1");

        internal void BreakProducerClosureEvaluation() =>
            Append("lean-report-inputs.json", "<");

        internal void Mutate(string mutation)
        {
            var path = mutation switch
            {
                "source" => "D5/Probe.lean",
                "toolchain" => "lean-toolchain",
                "lakefile" => "lakefile.toml",
                "manifest" => "lake-manifest.json",
                "inspector" => inspectorSourcePath,
                "inspector-script" => inspectorScriptPath,
                "input-helper" => InputHelperPath,
                "raw-report" => RawReportPath,
                "canonical-writer" => CanonicalWriterPath,
                "cache-fetcher" => CachePublishScriptPath,
                _ => throw new InvalidOperationException($"unknown mutation {mutation}"),
            };
            File.AppendAllText(
                Path.Combine(repository, path),
                "mutation\n",
                new UTF8Encoding(false));
        }

        internal ProcessOutput RunCommand(string command) => Run(command);

        internal void WriteSource(string relativePath, string contents) => Write(relativePath, contents);

        internal void InitializeGitRepository()
        {
            TestGit.Run(repository, "init", "--quiet");
            TestGit.Run(repository, "config", "user.email", "stratalint@example.invalid");
            TestGit.Run(repository, "config", "user.name", "StrataLint Tests");
            TestGit.Run(repository, "add", ".");
            TestGit.Run(repository, "commit", "--quiet", "-m", "lean input fixture");
        }

        internal void AssertMemoBehavior((string Sources, string Config) before)
        {
            if (OperatingSystem.IsWindows()) return;

            Assert.True(File.Exists(MemoFile));
            var memo = File.ReadAllBytes(MemoFile);
            Assert.Equal(before, CacheIdentity());

            PoisonSourceMemo();
            File.SetUnixFileMode(
                MemoRoot,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute
                    | UnixFileMode.GroupRead | UnixFileMode.GroupWrite | UnixFileMode.GroupExecute
                    | UnixFileMode.OtherRead | UnixFileMode.OtherWrite | UnixFileMode.OtherExecute);
            Assert.Equal(before, CacheIdentity());

            File.SetUnixFileMode(
                MemoRoot,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            foreach (var failure in new[] { "malformed", "unreadable" })
            {
                File.WriteAllBytes(MemoFile, memo);
                MakeMemoUnusable(failure);
                Assert.Equal(before, CacheIdentity());
                File.SetUnixFileMode(
                    MemoFile,
                    UnixFileMode.UserRead | UnixFileMode.UserWrite);
            }
            File.WriteAllBytes(MemoFile, memo);
        }

        [System.Runtime.Versioning.UnsupportedOSPlatform("windows")]
        internal void MakeMemoUnusable(string failure)
        {
            if (failure == "malformed")
            {
                File.WriteAllText(MemoFile, "not a memo\n", new UTF8Encoding(false));
                return;
            }

            Assert.Equal("unreadable", failure);
            File.SetUnixFileMode(MemoFile, 0);
        }

        internal void PoisonSourceMemo()
        {
            var sourceOid = TestGit.Run(
                    repository, "rev-parse", "HEAD:D5/Probe.lean")
                .Trim();
            var lines = File.ReadAllLines(MemoFile);
            var index = Array.FindIndex(
                lines,
                line => line.StartsWith(sourceOid + " ", StringComparison.Ordinal));
            Assert.True(index >= 0, "source blob is absent from memo");
            lines[index] = $"{sourceOid} {new string('0', 64)}";
            File.WriteAllLines(MemoFile, lines, new UTF8Encoding(false));
        }

        internal string Producer()
        {
            var result = Run("address");
            Assert.Equal(0, result.ExitCode);
            return Encoding.UTF8.GetString(result.StandardOutput)
                .Split(' ', StringSplitOptions.RemoveEmptyEntries)[1];
        }

        internal string Address()
        {
            var result = Run("address");
            Assert.Equal(0, result.ExitCode);
            return Encoding.UTF8.GetString(result.StandardOutput)
                .Split(' ', StringSplitOptions.RemoveEmptyEntries)[0];
        }

        internal (string Sources, string Config) CacheIdentity()
        {
            var result = Run("address");
            Assert.Equal(0, result.ExitCode);
            var parts = Encoding.UTF8.GetString(result.StandardOutput)
                .Split(' ', StringSplitOptions.RemoveEmptyEntries);
            Assert.Equal(4, parts.Length);
            return (parts[2], parts[3].Trim());
        }

        internal void Append(string relativePath, string contents) => File.AppendAllText(
            Path.Combine(repository, relativePath.Replace('/', Path.DirectorySeparatorChar)),
            contents,
            new UTF8Encoding(false));

        internal void RemoveInspectorDirectory() => Directory.Delete(
            Path.Combine(repository, "tools", "lean-inspector"),
            recursive: true);

        internal string ManifestHash(params string[] relativePaths)
        {
            var manifest = new StringBuilder();
            foreach (var relativePath in relativePaths)
            {
                var path = Path.Combine(
                    repository,
                    relativePath.Replace('/', Path.DirectorySeparatorChar));
                var digest = Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(path)));
                manifest.Append(digest).Append("  ").Append(relativePath).Append('\n');
            }
            return Convert.ToHexStringLower(
                SHA256.HashData(Encoding.UTF8.GetBytes(manifest.ToString())));
        }

        private ProcessOutput Run(string command, string? workingDirectory = null)
        {
            var arguments = new List<string>
            {
                $"STRATALINT_LEAN_INPUT_MEMO_ROOT={MemoRoot}",
            };
            arguments.AddRange(
            [
                "bash", script, command, "--repository", repository,
            ]);
            return TestProcessRunner.Run(
                "env",
                arguments,
                workingDirectory ?? temporary.Path,
                BoundedProcessRunner.HangDetectionBudget,
                1024 * 1024);
        }

        private void Write(string relativePath, string contents)
        {
            var path = Path.Combine(repository, relativePath.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, contents, new UTF8Encoding(false));
        }

        public void Dispose() => temporary.Dispose();

    }

    private static string[] Lines(ProcessOutput output) => Encoding.UTF8.GetString(output.StandardOutput)
        .Split('\n', StringSplitOptions.RemoveEmptyEntries);

    private static string[] Fields(ProcessOutput output) => Encoding.UTF8.GetString(output.StandardOutput)
        .Split(' ', StringSplitOptions.RemoveEmptyEntries);
}
