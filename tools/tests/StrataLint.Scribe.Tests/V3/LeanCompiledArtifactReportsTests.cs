using StrataLint.Engine;

namespace StrataLint.Scribe.Tests;

public sealed class LeanCompiledArtifactReportsTests
{
    [Fact]
    public void ScopedReportReadsRegisteredImportClosureWithoutUnselectedBodies()
    {
        using var fixture = new ScopedFixture();
        File.WriteAllBytes(fixture.Root.Resolve("D5/Unselected.lean"), [0xff]);
        File.WriteAllBytes(fixture.Root.Resolve("tools/Unregistered.lean"), [0xff]);

        var report = fixture.Read();

        Assert.Equal(["D5/Dependency.lean", "D5/Selected.lean"],
            report.Files.Keys.Select(path => path.Value).Order(StringComparer.Ordinal));
    }

    [Fact]
    public void ScopedReportRejectsChangedDependencyBytes()
    {
        using var fixture = new ScopedFixture();
        File.AppendAllText(fixture.Root.Resolve("D5/Dependency.lean"), "-- changed\n");

        Assert.Contains("source hash does not match D5/Dependency.lean",
            Assert.Throws<FormatException>(() => fixture.Read()).Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void ScopedReportCannotOmitManagedImportsThroughRegisteredSources(bool missingSource)
    {
        using var fixture = new ScopedFixture();
        if (missingSource) File.Delete(fixture.Root.Resolve("D5/Dependency.lean"));
        else File.WriteAllText(fixture.Root.Resolve(ScopedFixture.Bridge), "prelude\nimport D5.Missing\n");

        Assert.Contains(missingSource ? "D5.Dependency" : "D5.Missing",
            Assert.Throws<InvalidOperationException>(() => fixture.Read()).Message, StringComparison.Ordinal);
    }

    [Fact]
    public void ScopedReportPreservesCompleteLinkValidation()
    {
        using var fixture = new ScopedFixture();
        File.CreateSymbolicLink(fixture.Root.Resolve("unselected-link"), "D5/Dependency.lean");

        Assert.Contains("FILEMAP", Assert.Throws<InvalidOperationException>(() => fixture.Read()).Message,
            StringComparison.Ordinal);
    }

    private sealed class ScopedFixture : IDisposable
    {
        internal const string Bridge = "tools/lean-inspector-interface/LocalBridge/Catalog.lean";
        internal TemporaryRoot Root { get; } = new();

        internal ScopedFixture()
        {
            Directory.CreateDirectory(Root.Resolve(".git/objects"));
            Directory.CreateDirectory(Root.Resolve(".git/refs/heads"));
            File.WriteAllText(Root.Resolve(".git/HEAD"), "ref: refs/heads/fixture\n");
            File.WriteAllText(Root.Resolve(".git/config"), "[core]\nrepositoryformatversion = 0\nbare = false\n");
            File.WriteAllText(Root.Resolve(".gitignore"), "report.json*\n");
            var entries = new[]
            {
                RawRepositoryEntry.FromText("lean-report-inputs.json", """
                    {"dependency_sources":{"include":[
                      {"pattern":"tools/lean-inspector-interface/**/*.lean","optional":false}
                    ],"exclude":[]}}
                    """),
                RawRepositoryEntry.FromText("D5/Selected.lean", "prelude\nimport LocalBridge.Catalog\n"),
                RawRepositoryEntry.FromText(Bridge, "prelude\nimport D5.Dependency\n"),
                RawRepositoryEntry.FromText("D5/Dependency.lean", "prelude\n"),
            };
            foreach (var entry in entries) File.WriteAllBytes(Root.Resolve(entry.Path), entry.Bytes.AsSpan());
            var snapshot = Assert.IsType<SnapshotDecodeOutcome.Decoded>(SnapshotDecoder.Decode(
                RawRepositorySnapshot.Create(entries))).Snapshot;
            var reportPath = Root.Resolve("report.json");
            RawLeanReportArtifact.WriteFile(reportPath, snapshot, LeanAxiomReport.Create(
                new Dictionary<string, LeanFileReport>
                {
                    ["D5/Selected.lean"] = new(["LocalBridge.Catalog"], []),
                    ["D5/Dependency.lean"] = new([], []),
                }));
            File.WriteAllText(reportPath, File.ReadAllText(reportPath).Replace(
                RawLeanReportArtifact.Schema, RawLeanReportArtifact.ScopedSchema, StringComparison.Ordinal));
        }

        internal LeanAxiomReport Read() => LeanCompiledArtifactReports.ReadScopedRepositoryFiles(
            Root.Path, ["D5/Selected.lean"], "report.json");

        public void Dispose() => Root.Dispose();
    }

}
