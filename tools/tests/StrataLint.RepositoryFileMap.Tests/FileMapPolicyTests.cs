using System.Text;
using StrataLint.FileMap;
using StrataLint.Engine;

namespace StrataLint.RepositoryFileMap.Tests;

[Collection(nameof(CanonicalFileMapCollection))]
public sealed partial class FileMapPolicyTests(CanonicalFileMapFixture fixture)
{
    [Theory]
    [InlineData("tools/lean-inspector/scoped.py")]
    [InlineData("tools/scripts/report/lean-report-scoped.sh")]
    [InlineData("tools/tests/StrataLint.Lean.Tests/Native/lean_report_scoped_fixture.py")]
    public void ScopedReportProgramsHaveCanonicalFileMapEntries(string path)
    {
        var manifest = FileMapLoader.LoadRepository(TestRepositoryLayout.FindRoot());
        var entry = Assert.Single(manifest.Match(path));
        Assert.Equal(FileMapKind.Program, entry.Kind);
        Assert.Equal(FileMapAdmissionPlane.Judge, entry.AdmissionPlane);
    }

    [Theory]
    [InlineData("lean-report-inputs.json", "LeanReportSelection")]
    [InlineData("Meta/ci-cache-paths.json", "NativeArchivePaths")]
    [InlineData("Meta/ci-checks.json", "RegisteredCheckMaterials")]
    [InlineData("Meta/ci-units.json", "CiUnitContract")]
    public void RuntimeManifestIsAdmittedWithItsRuntimeVerifier(string path, string verifier)
    {
        var root = TestRepositoryLayout.FindRoot();
        var policy = Assert.IsType<PolicyLoadOutcome.Accepted>(
            RepositoryPolicyLoader.LoadRepository(root));
        Assert.Null(RepositoryPathPolicy.Validate(RepoPath.CreateKnown(path), policy.Policy));
        var manifest = FileMapLoader.LoadRepository(root);
        var entry = Assert.Single(manifest.Match(path));
        Assert.Equal(FileMapKind.Data, entry.Kind);
        Assert.Equal(FileMapAdmissionPlane.Judge, entry.AdmissionPlane);
        Assert.Equal(verifier, Assert.Single(entry.VerifiedBy));
        Assert.DoesNotContain(fixture.Findings, finding =>
            finding.Path == path && finding.Code is "FILEMAP-ACTOR-DANGLING"
                or "FILEMAP-DATA-VERIFIER" or "FILEMAP-DATA-VERIFIER-DANGLING");
    }

    [Fact]
    public void CanonicalFileMapPolicyReloadsWithArtifactKindsAndExactRootCoverage()
    {
        var root = TestRepositoryLayout.FindRoot();
        var policy = Assert.IsType<PolicyLoadOutcome.Accepted>(RepositoryPolicyLoader.LoadRepository(root)).Policy;
        var reloaded = Assert.IsType<PolicyLoadOutcome.Accepted>(RepositoryPolicyLoader.Load(
            policy.CanonicalFileMapBytes.AsSpan(),
            policy.CanonicalDomainsBytes.AsSpan())).Policy;

        Assert.Equal(policy.FileMapSha256, reloaded.FileMapSha256);
        Assert.Equal(policy.CanonicalFileMapBytes.ToArray(), reloaded.CanonicalFileMapBytes.ToArray());
        Assert.Equal(
            ["csv", "json", "md", "py", "txt", "yaml", "yml"],
            policy.ArtifactKinds.Keys.Select(static key => key.Value).Order(StringComparer.Ordinal).ToArray());
        Assert.Null(RepositoryPathPolicy.Validate(RepoPath.CreateKnown("LICENSE"), policy));
        Assert.NotNull(RepositoryPathPolicy.Validate(RepoPath.CreateKnown("unregistered.json"), policy));
        Assert.NotNull(RepositoryPathPolicy.Validate(RepoPath.CreateKnown("agents/unregistered.md"), policy));
    }

    [Theory]
    [InlineData("Meta/ReportConsumers/unregistered.json")]
    [InlineData("Meta/ReportConsumers/nested/lean-report.json")]
    [InlineData("Meta/unregistered.json")]
    public void UnregisteredMetaArtifactsRemainRejected(string path)
    {
        var root = TestRepositoryLayout.FindRoot();
        var policy = Assert.IsType<PolicyLoadOutcome.Accepted>(
            RepositoryPolicyLoader.LoadRepository(root));

        var issue = RepositoryPathPolicy.Validate(RepoPath.CreateKnown(path), policy.Policy);

        Assert.NotNull(issue);
        Assert.Equal("SL-000", issue.RuleId.Value);
        Assert.Contains("matches=0", issue.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void ComputationalProjectionsHaveCanonicalFileMapEntries()
    {
        var root = TestRepositoryLayout.FindRoot();
        var manifest = FileMapLoader.LoadRepository(root);
        var inventory = GeneratedArtifactInventory.Create(
            ["Blueprint/D5/S0/Carrier/Ring.md"]);
        var attestation = Assert.Single(inventory, artifact => artifact == GeneratedArtifactInventory.ScribeAttestation);
        var attestationEntry = Assert.Single(manifest.Match(attestation.Path));
        Assert.Equal(FileMapKind.Generated, attestationEntry.Kind);
        Assert.Equal(attestation.Producer, attestationEntry.ProducedBy);
        Assert.Contains(attestation.Producer, attestationEntry.VerifiedBy, StringComparer.Ordinal);

        const string pattern = "Blueprint/D5/S0/**/*.md";
        var entry = Assert.Single(manifest.Match("Blueprint/D5/S0/Carrier/Ring.md"));

        Assert.Equal(pattern, entry.Pattern);
        Assert.Equal(FileMapKind.Generated, entry.Kind);
        Assert.Equal("ScribeEmitter", entry.ProducedBy);
        Assert.Equal(
            ["ScribeEmitter", "reader"],
            entry.ConsumedBy.ToArray());
        Assert.Equal(["ScribeEmitter"], entry.VerifiedBy.ToArray());
        Assert.Contains(
            inventory,
            artifact => entry.Matches(artifact.Path));
        Assert.DoesNotContain(
            fixture.Findings,
            finding => finding.Path == pattern);
        Assert.DoesNotContain(fixture.Findings, finding => finding.Code == "FILEMAP-PATTERN-EMPTY");
    }

    [Fact]
    public void LibrarySplitBucketsAreClassifiedAsData()
    {
        var manifest = FileMapLoader.LoadRepository(TestRepositoryLayout.FindRoot());

        Assert.Equal(
            FileMapKind.Data,
            Assert.Single(manifest.Match("Library/Weil/sample2026paper.md")).Kind);
    }

}
