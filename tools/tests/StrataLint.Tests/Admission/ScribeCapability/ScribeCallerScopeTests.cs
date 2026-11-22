using StrataLint.Cli;
using StrataLint.Engine;

namespace StrataLint.Tests;

public sealed partial class ProductionEnvironmentTests
{
    [Fact]
    public void CheckCurrentScopeReadsTheProvidedNulSeparatedManifest()
    {
        using var temporary = new TemporaryDirectory();
        var fixture = new RuleFixture();
        fixture.Files["Meta/ci-checks.json"] = CommonCheckRegistrationFixture.Manifest(
            "tools/StrataLint.Scribe/StrataLint.Scribe.csproj");
        InstallScopedScribeRegistration(fixture.Files, temporary.Path);
        var snapshot = Snapshot(fixture.Files);
        var paths = Path.Combine(temporary.Path, "paths");
        File.WriteAllText(paths, "Library/notes/Probe.md\0notes/selected.txt\0");
        var verifier = new FakeScribeEmissionVerifier(VerifiedScribeEmissions.Empty);
        var environment = new ProductionCliEnvironment(temporary.Path,
            new FakeRepositoryGateway(RawChangeSet.Create([]), snapshot, Snapshot(fixture.Baseline)),
            new FakeLeanReportSource(null), verifier);

        var result = environment.CheckCurrent(["--changed-paths-from", paths]);

        Assert.Equal(0, result.ExitCode);
        Assert.Equal(["Library/notes/Probe.md", "notes/selected.txt"],
            Assert.Single(verifier.Scopes).Paths.Select(path => path.Value).ToArray());
    }

    [Theory]
    [InlineData(1, "describe red code=probe", false)]
    [InlineData(2, "HostConfiguration: probe", true)]
    public void CheckCurrentReportsScribeVerificationFailuresWithTheirOwnExitCode(
        int exitCode, string message, bool infrastructure)
    {
        using var temporary = new TemporaryDirectory();
        var fixture = new RuleFixture();
        fixture.Files["Meta/ci-checks.json"] = CommonCheckRegistrationFixture.Manifest(
            "tools/StrataLint.Scribe/StrataLint.Scribe.csproj");
        InstallScopedScribeRegistration(fixture.Files, temporary.Path);
        var snapshot = Snapshot(fixture.Files);
        var paths = Path.Combine(temporary.Path, "paths");
        File.WriteAllText(paths, "Library/notes/Probe.md\0");
        var environment = new ProductionCliEnvironment(temporary.Path,
            new FakeRepositoryGateway(RawChangeSet.Create([]), snapshot, Snapshot(fixture.Baseline)),
            new FakeLeanReportSource(null), new RejectingScribeEmissionVerifier(exitCode, message));

        var result = environment.CheckCurrent(["--changed-paths-from", paths]);

        Assert.Equal(exitCode, result.ExitCode);
        Assert.Contains(message, result.Error, StringComparison.Ordinal);
        Assert.Equal(infrastructure, result.Error.StartsWith("INFRASTRUCTURE_FAILURE ", StringComparison.Ordinal));
    }

    private static void InstallScopedScribeRegistration(Dictionary<string, string> files, string root)
    {
        const string registration = "Meta/ReportProducers/scribe-content.json";
        const string inputs = "lean-report-inputs.json";
        files[registration] = """
            {"schema":"report-producer-scope-v2","registration":"lean-report-inputs.json",
             "scope":"scribe-content","projects":[]}
            """;
        files[inputs] = """
            {"inspector_sources":{"include":[],"exclude":[]},"producer_scopes":{
             "lean-report":{"include":[],"exclude":[]},"scribe-content":{"include":[],"exclude":[]}}}
            """;
        Directory.CreateDirectory(Path.Combine(root, "Meta/ReportProducers"));
        File.WriteAllText(Path.Combine(root, registration), files[registration]);
        File.WriteAllText(Path.Combine(root, inputs), files[inputs]);
    }

    private sealed class RejectingScribeEmissionVerifier(int exitCode, string message) : IScribeEmissionVerifier
    {
        public VerifiedScribeEmissions Verify(RepositorySnapshot snapshot, LeanAxiomReport report, RawChangeSet? changes,
            FrozenStateCatalog? frozenState = null, FrozenStatementIndex? frozenStatements = null) =>
            throw new ScribeVerificationException(exitCode, message);
    }

}
