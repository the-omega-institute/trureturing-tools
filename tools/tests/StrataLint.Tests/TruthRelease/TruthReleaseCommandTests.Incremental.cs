using System.Text.Json;
using System.Collections.Immutable;
using StrataLint.Engine;
using Trureturing.Truth;

namespace StrataLint.Tests;

public sealed partial class TruthReleaseCommandTests
{
    [Theory]
    [InlineData("unchanged", 3)]
    [InlineData("strict", 2)]
    [InlineData("blueprint", 3)]
    [InlineData("filemap", 3)]
    [InlineData("scribe", 3)]
    [InlineData("history", 0)]
    public void IncrementalBundleMatchesColdProjection(string change, int expectedReused)
    {
        using var fixture = CreateFixture(annotatedFrozenState: true, pairedFrozenState: true, additionalLocalSource: true);
        using var prior = new TemporaryDirectory();
        var original = Run(fixture, prior.Path, GreenTrustArguments());
        Assert.True(original.ExitCode == 0, original.Console.Error);
        File.WriteAllText(Path.Combine(prior.Path, "reuse.json"), JsonSerializer.Serialize(new[]
        {
            "D5.S0.Carrier.Dependency", BlueprintGid.Replace('/', '.'), TetrahedronPath[..^5].Replace('/', '.'),
        }));
        var root = fixture.RepositoryRoot;
        TestGit.Run(root, "restore", ".");
        if (change == "strict")
        {
            var path = Path.Combine(root, FrozenStatePath.Root + TetrahedronPath + ".json");
            var frozen = FrozenStateCatalog.Load(Snapshot(fixture)).Records[RepoPath.CreateKnown(TetrahedronPath)];
            var declarations = frozen.Declarations.Select(d => d.StrictOmega is { } strict
                ? d with { StrictOmega = strict with { Libraries = ["OmegaInit"] } } : d).ToImmutableArray();
            File.WriteAllBytes(path, FrozenStateRecord.Encode(frozen.StatementId, declarations).ToArray());
            CommitAll(root, change);
        }
        if (change == "blueprint")
        {
            File.AppendAllText(Path.Combine(root, "Blueprint", BlueprintGid + ".md"), "Extra projection text.\n");
            CommitAll(root, change);
        }
        if (change == "filemap")
        {
            var path = Path.Combine(root, "Meta", "FILEMAP.toml");
            File.WriteAllText(path, File.ReadAllText(path).Replace("ScribeCompiler", "ChangedCompiler", StringComparison.Ordinal));
            CommitAll(root, change);
        }
        if (change == "history")
        {
            var path = Path.Combine(prior.Path, TruthReleaseBundleWriter.PublicationFileName);
            var older = TruthReleasePublicationReader.Read(File.ReadAllBytes(path)) with { ProducerCommit = new string('a', 40) };
            File.WriteAllBytes(path, TruthReleasePublicationJsonWriter.Write(older).ToArray());
        }
        string[]? pack = null;
        using var resources = new TemporaryDirectory();
        if (change == "scribe")
        {
            var path = Path.Combine(resources.Path, "other.zip");
            var digest = ScribeResourcePack.Write(path, [SimpleDefinition(BlueprintGid),
                SimpleDefinition("D5/S0/Carrier/LocalOnly")]).TotalSha256;
            pack = PackArguments(path, digest);
        }
        CompareBuilds(fixture, prior.Path, expectedReused, pack);
    }

    [Fact]
    public void RebuildsChangedPrerequisitesThroughTwoLevelsAndAddsAndRemovesModules()
    {
        using var fixture = CreateFixture(annotatedFrozenState: true, pairedFrozenState: true);
        using var prior = new TemporaryDirectory();
        using var reportsDirectory = new TemporaryDirectory();
        var root = fixture.RepositoryRoot;
        TestGit.Run(root, "restore", ".");
        var reports = RawLeanReportArtifact.ReadFile(fixture.ReportPath, Snapshot(fixture)).Files
            .ToDictionary(pair => pair.Key.Value, pair => pair.Value, StringComparer.Ordinal);
        var dependant = "D5/S0/Carrier/Grandchild.lean";
        var removed = "D5/S0/Carrier/Removed.lean";
        var added = "D5/S0/Carrier/Added.lean";
        var dependency = "D5/S0/Carrier/Dependency.lean";
        var marker = BlueprintGid.Replace('/', '.');
        File.WriteAllText(Path.Combine(root, dependant), $"import {marker}\ntheorem grandchild : True := by trivial\n");
        reports[dependant] = new([marker], [Declaration("grandchild")]);
        File.WriteAllText(Path.Combine(root, removed), "theorem removed : True := by trivial\n");
        reports[removed] = new([], [Declaration("removed")]);
        CommitAll(root, "base modules");
        WriteReport();
        var baseline = Run(fixture, prior.Path, GreenTrustArguments());
        Assert.True(baseline.ExitCode == 0, baseline.Console.Error);

        File.Delete(Path.Combine(root, removed));
        reports.Remove(removed);
        File.WriteAllText(Path.Combine(root, added), "theorem added : True := by trivial\n");
        reports[added] = new([], [Declaration("added")]);
        File.WriteAllText(Path.Combine(root, dependency), "theorem changed : True := by trivial\n");
        reports[dependency] = new([], [Declaration("changed")]);
        CommitAll(root, "replace dependency statement and selected modules");
        WriteReport();
        File.WriteAllText(Path.Combine(prior.Path, "reuse.json"), JsonSerializer.Serialize(
            new[] { marker, dependant[..^5].Replace('/', '.'), TetrahedronPath[..^5].Replace('/', '.') }));
        CompareBuilds(fixture, prior.Path, 1);

        void WriteReport()
        {
            var candidate = Path.Combine(reportsDirectory.Path, GitObject(root, "HEAD") + ".json");
            RawLeanReportArtifact.WriteFile(candidate, Snapshot(fixture), LeanAxiomReport.Create(reports));
            fixture.ReportPath = candidate;
        }
    }

    private static RepositorySnapshot Snapshot(Fixture fixture) =>
        Assert.IsType<SnapshotDecodeOutcome.Decoded>(SnapshotDecoder.Decode(
            fixture.Gateway.ReadRevision(GitObject(fixture.RepositoryRoot, "HEAD")))).Snapshot;

    private static void CompareBuilds(Fixture fixture, string prior, int expectedReused, string[]? pack = null)
    {
        using var cold = new TemporaryDirectory();
        using var warm = new TemporaryDirectory();
        using var dags = new TemporaryDirectory();
        var coldDag = Path.Combine(dags.Path, "cold.md");
        var warmDag = Path.Combine(dags.Path, "warm.md");
        var reference = Run(fixture, cold.Path, [.. GreenTrustArguments(), "--dag-output", coldDag], pack);
        Assert.True(reference.ExitCode == 0, reference.Console.Error);
        var incremental = Run(fixture, warm.Path, [.. GreenTrustArguments(), "--base-release", prior, "--dag-output", warmDag], pack);
        Assert.True(incremental.ExitCode == 0, incremental.Console.Error);
        Assert.Contains($"reused_export_nodes={expectedReused} ", incremental.Console.Output);
        Assert.Equal(File.ReadAllBytes(coldDag), File.ReadAllBytes(warmDag));
        var files = Directory.GetFiles(cold.Path).Select(Path.GetFileName).Order(StringComparer.Ordinal).ToArray();
        Assert.Equal(8, files.Length);
        Assert.Equal(files, Directory.GetFiles(warm.Path).Select(Path.GetFileName).Order(StringComparer.Ordinal));
        foreach (var name in files)
            Assert.True(File.ReadAllBytes(Path.Combine(cold.Path, name!)).SequenceEqual(File.ReadAllBytes(Path.Combine(warm.Path, name!))), name);
    }
}
