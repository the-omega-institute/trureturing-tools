using System.Text;
using System.Text.Json;
using StrataLint.Cli;
using StrataLint.Engine;

namespace StrataLint.CliIntegration.Tests;

public sealed class PairedAdmissionTests
{
    private const string ProtectedBase = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    [Theory]
    [InlineData(false, false, 0, 1)]
    [InlineData(false, true, 1, 1)]
    [InlineData(true, false, 2, 2)]
    public void PairedChecksMatchStandaloneResultsAndReuseSuccessfullyVerifiedEvidence(
        bool staleReport, bool rejectCurrent, int expectedExit, int expectedReportReads)
    {
        using var temporary = new TemporaryDirectory();
        var fixture = CreateFixture();
        const string source = "Reg/D5/S0/Carrier/ScopedEvidence.lean";
        fixture.Files[source] = fixture.Baseline[source] = "prelude\n";
        var module = LeanImportClosure.ModuleName(RepoPath.CreateKnown(source));
        var raw = Snapshot(fixture.Files);
        var sourceOnly = Decode(RawRepositorySnapshot.Create([RawRepositoryEntry.FromText(source, fixture.Files[source])]));
        var artifact = Path.Combine(temporary.Path, "report.json");
        RawLeanReportArtifact.WriteFile(artifact, sourceOnly, LeanAxiomReport.Create(new Dictionary<string, LeanFileReport>
        {
            [source] = new([], []),
        }));
        File.WriteAllText(artifact, File.ReadAllText(artifact).Replace(
            RawLeanReportArtifact.Schema, RawLeanReportArtifact.ScopedSchema, StringComparison.Ordinal));
        File.WriteAllText(artifact + ".sha256", "fixture");
        var attestation = artifact + ".input.attestation";
        File.WriteAllText(attestation, staleReport ? "stale\n" : "verified\n");
        File.WriteAllText(artifact + ".provenance.json", JsonSerializer.Serialize(new
        {
            inputs = new { scope = new { roots = new[] { module } } },
            module_origins = new Dictionary<string, object> { [module] = new { report_sha256 = module } },
        }));
        var scripts = Directory.CreateDirectory(Path.Combine(temporary.Path, "tools/scripts/report")).FullName;
        var verifierLog = Path.Combine(temporary.Path, "verification.log");
        File.WriteAllText(Path.Combine(scripts, "lean-report-input.sh"), """
            set -eu
            test "$1" = verify-scoped
            echo "$7" >> "$3/verification.log"
            if ! grep -qx verified "$5.input.attestation"; then
                echo 'stale scoped input/provenance' >&2
                exit 2
            fi
            """ + "\n");
        var changed = Path.Combine(temporary.Path, "changed");
        File.WriteAllText(changed, source + "\0");
        var reports = Path.Combine(temporary.Path, "reports");
        File.WriteAllText(reports, artifact + "\0");
        string[] arguments = ["--changed-paths-from", changed, "--scoped-reports-from", reports];
        var changes = RawChangeSet.Create([source]);
        var gateway = new FakeRepositoryGateway(changes, raw, Snapshot(fixture.Baseline));
        var verifier = rejectCurrent
            ? new ProductionScribeEmissionVerifier((_, _, _, _) => throw new ScribeVerificationException(1, "scribe rejected"))
            : null;
        var environment = new ProductionCliEnvironment(temporary.Path, gateway, new FakeLeanReportSource(null), verifier);

        var previousReading = RawLeanReportArtifact.Reading.Value;
        var reportReads = 0;
        RawLeanReportArtifact.Reading.Value = () => reportReads++;
        try
        {
            var paired = Run(["check-current-and-delta", "--protected-base", ProtectedBase, .. arguments]);
            Assert.True(paired.Exit == expectedExit, paired.Output + paired.Error);
            Assert.Equal(expectedReportReads, reportReads);
            Assert.Equal(Enumerable.Repeat(module, expectedReportReads), File.ReadAllLines(verifierLog));
            Assert.Equal(1, gateway.ReadCurrentCount);
            Assert.Equal(1, gateway.PrepareCount);

            var current = Run(["check-current", .. arguments]);
            var delta = Run(["check-delta", "--protected-base", ProtectedBase, .. arguments]);
            Assert.Equal(staleReport ? 2 : 0, delta.Exit);
            Assert.Equal(expectedExit, current.Exit);
            Assert.Equal(current.Output + delta.Output, paired.Output);
            Assert.Equal(current.Error + delta.Error, paired.Error);
            Assert.Equal(expectedReportReads + 2, reportReads);

            // The same environment must validate fresh input on the next command.
            File.WriteAllText(attestation, staleReport ? "verified\n" : "stale\n");
            var next = Run(["check-current-and-delta", "--protected-base", ProtectedBase, .. arguments]);
            Assert.True(next.Exit == (staleReport ? 0 : 2), next.Output + next.Error);
            Assert.Equal(staleReport ? "" : "INFRASTRUCTURE_FAILURE stale scoped input/provenance\n"
                + "INFRASTRUCTURE_FAILURE stale scoped input/provenance\n", next.Error);
        }
        finally
        {
            RawLeanReportArtifact.Reading.Value = previousReading;
        }

        (int Exit, string Output, string Error) Run(string[] command)
        {
            var console = new BufferedConsole();
            var exit = CliApplication.Run(command, environment, console);
            return (exit, console.Output, console.Error);
        }
    }

    [Theory]
    [InlineData("mismatch", 2, "changed-path manifest differs from the protected-base delta")]
    [InlineData("invalid-base", 2, "delta requires an explicit 40-hex base commit SHA")]
    [InlineData("malformed", 2, "expected a NUL-terminated path manifest")]
    [InlineData("protected", 3, "protected-surface change detected (SL-022)")]
    public void PairedCliMatchesStandaloneDiagnosticsAgainstAGitRepository(string scenario, int deltaExit, string message)
    {
        using var temporary = new TemporaryDirectory();
        var root = Directory.CreateDirectory(Path.Combine(temporary.Path, "candidate")).FullName;
        var fixture = CreateFixture();
        var pathToChange = scenario == "protected" ? "Directory.Build.props" : "Library/notes/Fixture.md";
        fixture.Files.TryAdd(pathToChange, "base\n");
        foreach (var (path, contents) in fixture.Files)
        {
            var file = Path.Combine(root, path);
            Directory.CreateDirectory(Path.GetDirectoryName(file)!);
            File.WriteAllText(file, contents);
        }
        Git("init", "-q");
        Git("add", ".");
        Git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base");
        var baseline = scenario == "invalid-base" ? "invalid" : Git("rev-parse", "HEAD").Trim();
        File.AppendAllText(Path.Combine(root, pathToChange), "\n<!-- candidate -->\n");
        var changed = Path.Combine(temporary.Path, "changed");
        File.WriteAllText(changed, scenario switch
        {
            "mismatch" => "",
            "malformed" => pathToChange,
            _ => pathToChange + "\0",
        });
        string[] arguments = ["--changed-paths-from", changed];
        var current = Run(["check-current", .. arguments]);
        var delta = Run(["check-delta", "--protected-base", baseline, .. arguments]);
        var paired = Run(["check-current-and-delta", "--protected-base", baseline, .. arguments]);
        Assert.True(current.Exit == (scenario == "malformed" ? 2 : 0), current.Output + current.Error);
        Assert.True(delta.Exit == deltaExit, delta.Output + delta.Error);
        Assert.Equal(deltaExit == 3 ? 0 : deltaExit, paired.Exit);
        Assert.Contains(message, deltaExit == 3 ? delta.Output : delta.Error);
        Assert.Equal(current.Output + delta.Output, paired.Output);
        Assert.Equal(current.Error + delta.Error, paired.Error);

        string Git(params string[] args)
        {
            var result = TestProcessRunner.Run("git", args, root, TestBudgets.ScriptProcessHangGuard, 64 * 1024);
            Assert.True(result.ExitCode == 0, Encoding.UTF8.GetString(result.StandardError));
            return Encoding.UTF8.GetString(result.StandardOutput);
        }

        (int Exit, string Output, string Error) Run(string[] command)
        {
            var result = TestProcessRunner.Run("dotnet", [typeof(Program).Assembly.Location, .. command],
                root, TestBudgets.ScriptProcessHangGuard, 64 * 1024);
            return (result.ExitCode, Encoding.UTF8.GetString(result.StandardOutput), Encoding.UTF8.GetString(result.StandardError));
        }
    }

    private static RuleFixture CreateFixture()
    {
        var fixture = new RuleFixture();
        foreach (var files in new[] { fixture.Files, fixture.Baseline })
        {
            files["Meta/ReportProducers/scribe-content.json"] = """
                {"schema":"report-producer-scope-v2","registration":"lean-report-inputs.json",
                 "scope":"scribe-content","projects":[]}
                """;
            files["lean-report-inputs.json"] = """
                {"inspector_sources":{"include":[],"exclude":[]},"producer_scopes":{
                 "lean-report":{"include":[],"exclude":[]},"scribe-content":{"include":[],"exclude":[]}}}
                """;
        }
        return fixture;
    }

    private static RawRepositorySnapshot Snapshot(Dictionary<string, string> files) =>
        RawRepositorySnapshot.Create(files.Select(static pair => RawRepositoryEntry.FromText(pair.Key, pair.Value)));

    private static RepositorySnapshot Decode(RawRepositorySnapshot raw) =>
        Assert.IsType<SnapshotDecodeOutcome.Decoded>(SnapshotDecoder.Decode(raw)).Snapshot;
}
