using System;
using System.Collections.Immutable;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Trureturing.Truth;
using Xunit;

namespace StrataLint.Tests;

public sealed class TruthReleaseVerificationTests
{
    private static readonly UTF8Encoding Utf8 = new(false, true);
    private const string Commit = "1111111111111111111111111111111111111111";
    private const string OtherCommit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    private const string Tree = "2222222222222222222222222222222222222222";
    private const string OtherTree = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    private const string Repo = "the-omega-institute/trureturing";
    private const string OtherRepo = "the-omega-institute/some-other-repo";
    private const string OfficialTypeAddress = "sha256:3333333333333333333333333333333333333333333333333333333333333333";
    private const string OtherTypeAddress = "sha256:4444444444444444444444444444444444444444444444444444444444444444";

    private static string Digest(byte[] bytes) =>
        "sha256:" + Convert.ToHexStringLower(SHA256.HashData(bytes));

    private static byte[] MinimalTruthGraphBytes(string contentDigest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") =>
        TruthGraphJsonWriter.Write(new TruthGraphExportModel(
            TruthGraphExportModel.Dialect,
            1,
            new TruthGraphProvenance(
                contentDigest,
                "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
            {
                TruthRootSha256 = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                DependencyGranularity = "module-import",
            },
            new TruthGraphSection(
                ImmutableArray<TruthGraphNode>.Empty,
                ImmutableArray<TruthGraphEdge>.Empty,
                ImmutableArray<TruthGraphOpenBlocker>.Empty,
                new TruthGraphStateCounts(0, 0, 0, 0)),
            new DocumentGraphSection(
                ImmutableArray<DocumentGraphNode>.Empty,
                ImmutableArray<DescribeGraphNode>.Empty,
                ImmutableArray<DocumentDependencyEdge>.Empty,
                ImmutableArray<DocumentNarrativeReferenceEdge>.Empty),
            new TruthGraphJoinsSection(ImmutableArray<TruthAnchorJoin>.Empty),
            ImmutableArray.Create("digestion"))).ToArray();

    private static byte[] TruthExportBytes(string commit, string tree) =>
        TruthExportJsonWriter.Write(TruthExportModel.Create(
            ImmutableArray.Create(new TruthExportNode(
                "D5/S0/A.lean",
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ImmutableArray.Create("propext"),
                ImmutableArray.Create(new TruthExportDeclaration(
                    "nk-a",
                    "theorem",
                    "sha256:1111111111111111111111111111111111111111111111111111111111111111")),
                ImmutableArray<string>.Empty,
                "frozen")),
            commit,
            tree)).ToArray();

    private static byte[] SourceSnapshotBytes(
        string commit,
        string tree,
        string truthGraphDigest,
        string rawLeanReportDigest,
        string declarationsDigest) =>
        Utf8.GetBytes($$"""
            {
              "schema": "source-snapshot",
              "source_repo": "the-omega-institute/trureturing",
              "source_commit": "{{commit}}",
              "source_tree": "{{tree}}",
              "lean_toolchain": "leanprover/lean4:v4.24.0",
              "mathlib_rev": "3333333333333333333333333333333333333333",
              "producer_package_commit": "4444444444444444444444444444444444444444",
              "truth_graph_sha256": "{{truthGraphDigest}}",
              "raw_lean_report_sha256": "{{rawLeanReportDigest}}",
              "dag_md_sha256": "sha256:6666666666666666666666666666666666666666666666666666666666666666",
              "declarations_sha256": "{{declarationsDigest}}"
            }
            """);

    private static (string Directory, string Digest) BuildBundle(
        string snapshotCommit = Commit,
        string exportCommit = Commit,
        string manifestCommit = Commit,
        string snapshotTree = Tree,
        string exportTree = Tree,
        string manifestTree = Tree,
        string manifestRepo = Repo,
        string? snapshotTruthGraphDigest = null,
        byte[]? truthGraph = null,
        byte[]? truthExport = null,
        byte[]? rawLeanReport = null)
    {
        truthGraph ??= MinimalTruthGraphBytes();
        truthExport ??= TruthExportBytes(exportCommit, exportTree);
        rawLeanReport ??= Utf8.GetBytes("raw_lean_report");
        var sourceSnapshot = SourceSnapshotBytes(
            snapshotCommit,
            snapshotTree,
            snapshotTruthGraphDigest ?? Digest(truthGraph),
            Digest(rawLeanReport),
            Digest(truthExport));
        var artifacts = new (string Key, string File, byte[] Bytes)[]
        {
            ("source_snapshot", "source-snapshot.v1.json", sourceSnapshot),
            ("truth_graph", "truth-graph.v1.json", truthGraph),
            ("raw_lean_report", "raw-lean-report.json", rawLeanReport),
            ("truth_export", "truth-export.v1.json", truthExport),
            ("blueprint_index", "blueprint-index.v1.json", Utf8.GetBytes("blueprint_index")),
        };

        var sums = string.Concat(artifacts
            .OrderBy(static artifact => artifact.File, StringComparer.Ordinal)
            .Select(artifact => Digest(artifact.Bytes)["sha256:".Length..] + "  " + artifact.File + "\n"));
        var releaseDigest = Digest(Utf8.GetBytes(sums));
        var artifactJson = string.Join(",\n", artifacts.Select(artifact =>
            $"    \"{artifact.Key}\": {{ \"file\": \"{artifact.File}\", \"sha256\": \"{Digest(artifact.Bytes)}\" }}"));
        var manifest = $$"""
            {
              "schema": "truth-release",
              "source": {
                "source_repo": "{{manifestRepo}}",
                "source_commit": "{{manifestCommit}}",
                "source_tree": "{{manifestTree}}"
              },
              "trust": {
                "commit_on_protected_dev": true,
                "required_checks": [
                  { "name": "engineering", "conclusion": "success" },
                  { "name": "current", "conclusion": "success" }
                ]
              },
              "producer": {
                "package_repo": "the-omega-institute/trureturing-fkst-packages",
                "package_commit": "4444444444444444444444444444444444444444",
                "read_only": true
              },
              "artifacts": {
            {{artifactJson}}
              },
              "sha256sums_digest": "{{releaseDigest}}",
              "produced_at": "2026-08-22T00:00:00Z"
            }
            """;

        var directory = Directory.CreateTempSubdirectory("truthverify").FullName;
        foreach (var artifact in artifacts)
        {
            File.WriteAllBytes(Path.Combine(directory, artifact.File), artifact.Bytes);
        }

        File.WriteAllText(Path.Combine(directory, "SHA256SUMS"), sums, Utf8);
        File.WriteAllText(Path.Combine(directory, "release-manifest.v1.json"), manifest, Utf8);
        return (directory, releaseDigest);
    }

    [Fact]
    public void VerifiesACoherentBundle()
    {
        var (directory, digest) = BuildBundle();
        try
        {
            var verified = TruthReleaseVerification.Verify(directory, digest);

            Assert.Equal(digest, verified.ReleaseDigest);
            Assert.Equal("truth-export.v1.json", verified.Manifest.Artifacts.TruthExport.File);
            Assert.Equal(Commit, verified.Manifest.Source.SourceCommit);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void RejectsSnapshotCommitFromAnotherRevision() =>
        AssertCompositionRejected(() => BuildBundle(snapshotCommit: OtherCommit));

    [Fact]
    public void RejectsTruthExportCommitFromAnotherRevision() =>
        AssertCompositionRejected(() => BuildBundle(exportCommit: OtherCommit));

    [Fact]
    public void RejectsManifestCommitFromAnotherRevision() =>
        AssertCompositionRejected(() => BuildBundle(manifestCommit: OtherCommit));

    [Fact]
    public void RejectsSnapshotTreeFromAnotherRevision() =>
        AssertCompositionRejected(() => BuildBundle(snapshotTree: OtherTree));

    [Fact]
    public void RejectsTruthExportTreeFromAnotherRevision() =>
        AssertCompositionRejected(() => BuildBundle(exportTree: OtherTree));

    [Fact]
    public void RejectsManifestTreeFromAnotherRevision() =>
        AssertCompositionRejected(() => BuildBundle(manifestTree: OtherTree));

    [Fact]
    public void RejectsManifestRepoFromAnotherRepository() =>
        // The manifest bytes are not SHA256SUMS-covered, so rewriting only the manifest's source_repo
        // leaves every artifact hash and the release digest intact. Composition must still reject it by
        // binding the manifest repo to the SHA-covered source_snapshot.
        AssertCompositionRejected(() => BuildBundle(manifestRepo: OtherRepo));

    [Fact]
    public void RejectsSnapshotDigestNamingADifferentValidTruthGraph()
    {
        var otherGraph = MinimalTruthGraphBytes(
            "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd");
        Assert.Equal(TruthGraphExportModel.Dialect, TruthGraphJsonReader.Read(otherGraph).Schema);

        AssertCompositionRejected(() => BuildBundle(snapshotTruthGraphDigest: Digest(otherGraph)));
    }

    [Fact]
    public void RejectsACorrectlyHashedMalformedTruthGraph() =>
        AssertCompositionRejected(() => BuildBundle(truthGraph: Utf8.GetBytes("{}")));

    [Fact]
    public void RejectsACorrectlyHashedMalformedTruthExport() =>
        AssertCompositionRejected(() => BuildBundle(truthExport: Utf8.GetBytes("{}")));

    [Fact]
    public void RejectsACorrectlyHashedExportMarkerThatContradictsItsAnnotations()
    {
        var original = Utf8.GetString(TruthExportBytes(Commit, Tree));
        var tampered = StructuredCanonicalWriter.WriteJson(original.Replace("\"declaration_name_key\":",
            "\"libraries\": [\"OmegaMath\"], \"omega_only_axiom_free\": true, \"declaration_name_key\":",
            StringComparison.Ordinal));
        AssertCompositionRejected(() => BuildBundle(truthExport: tampered.ToArray()));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("sha256:2222222222222222222222222222222222222222222222222222222222222222")]
    public void RejectsADigestBoundGraphWithMissingOrMismatchedDeclarationMarkers(string? statementId)
    {
        var export = TruthExportJsonReader.Read(TruthExportBytes(Commit, Tree));
        var node = Assert.Single(export.Nodes);
        var declaration = Assert.Single(node.Declarations) with
        {
            Libraries = ["OmegaMath"], Axioms = [], OmegaOnlyAxiomFree = true,
        };
        var exportBytes = TruthExportJsonWriter.Write(export with { Nodes = [node with { Declarations = [declaration] }] });
        var graph = TruthGraphJsonReader.Read(MinimalTruthGraphBytes());
        var graphNode = new TruthGraphNode(node.RepoPath, null, "closed", null, 0)
        {
            OmegaOnlyAxiomFreeDeclarations = statementId is null ? null
                : [new TruthGraphDeclaration(declaration.DeclarationNameKey, declaration.Kind, statementId)],
        };
        var graphBytes = TruthGraphJsonWriter.Write(graph with
        {
            Truth = graph.Truth with { Nodes = [graphNode], StateCounts = new(1, 0, 0, 0) },
        });
        AssertCompositionRejected(() => BuildBundle(truthGraph: graphBytes.ToArray(), truthExport: exportBytes.ToArray()));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void RejectsDigestBoundGraphWithMissingOrDifferentStrictMeasurement(bool includeDifferentPair)
    {
        var (graph, export) = PairedArtifacts(includeDifferentPair ? "different" : "absent");
        AssertCompositionRejected(() => BuildBundle(truthGraph: graph, truthExport: export,
            rawLeanReport: PairedRawReport()));
    }

    [Fact]
    public void VerifiesStrictMeasurementWithMatchingRawStatementTypeAddress()
    {
        var (graph, export) = PairedArtifacts();
        var (directory, digest) = BuildBundle(truthGraph: graph, truthExport: export, rawLeanReport: PairedRawReport());
        try
        {
            var verified = TruthReleaseVerification.Verify(directory, digest);
            var declaration = Assert.Single(Assert.Single(verified.ReadTruthExport().Nodes).Declarations);
            Assert.Equal(OfficialTypeAddress, declaration.StrictOmega!.OfficialStatementTypeAddress);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Theory]
    [InlineData("type address")]
    [InlineData("missing type address")]
    [InlineData("name key")]
    [InlineData("kind")]
    [InlineData("module")]
    [InlineData("source path")]
    [InlineData("absent")]
    [InlineData("historical")]
    public void RejectsStrictMeasurementWithoutMatchingRawStatement(string difference)
    {
        var (graph, export) = PairedArtifacts();
        var report = difference switch
        {
            "type address" => PairedRawReport(typeAddress: OtherTypeAddress),
            "missing type address" => PairedRawReport(typeAddress: null),
            "name key" => PairedRawReport(nameKey: "nk-A"),
            "kind" => PairedRawReport(kind: "def"),
            "module" => PairedRawReport(module: "D5.S0.B"),
            "source path" => PairedRawReport(sourcePath: "D5/S0/B.lean"),
            "absent" => Utf8.GetBytes("""{"modules":[],"schema":"stratalint-raw-lean-report-v4"}"""),
            "historical" => Utf8.GetBytes("""{"modules":[],"schema":"stratalint-raw-lean-report-v3"}"""),
            _ => throw new ArgumentOutOfRangeException(nameof(difference)),
        };
        var (directory, digest) = BuildBundle(truthGraph: graph, truthExport: export, rawLeanReport: report);
        try
        {
            var error = Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
            Assert.Contains("raw_lean_report", error.Message, StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void VerifiesUnannotatedBundleWithHistoricalRawReport()
    {
        var report = Utf8.GetBytes("""{"modules":[],"schema":"stratalint-raw-lean-report-v3"}""");
        var (directory, digest) = BuildBundle(rawLeanReport: report);
        try
        {
            Assert.Equal(digest, TruthReleaseVerification.Verify(directory, digest).ReleaseDigest);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void RejectsTamperedRawReportEvenWhenThePairedStatementMatches()
    {
        var (graph, export) = PairedArtifacts();
        var (directory, digest) = BuildBundle(truthGraph: graph, truthExport: export, rawLeanReport: PairedRawReport());
        try
        {
            File.WriteAllBytes(Path.Combine(directory, TruthReleaseBundleWriter.RawLeanReportFileName),
                PairedRawReport(ignoredMetadata: "[false]"));

            var error = Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
            Assert.Contains("content does not match its recorded sha256", error.Message, StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void VerifiesLargePairedRawReportWithoutAllocatingTheWholeReport()
    {
        var (graph, export) = PairedArtifacts();
        var report = PairedRawReport(ignoredMetadata: "[\"" + new string('x', 70_000) + "\","
            + string.Concat(Enumerable.Repeat("[false,1234],", 850_000)) + "null]");
        var (directory, digest) = BuildBundle(truthGraph: graph, truthExport: export, rawLeanReport: report);
        try
        {
            var before = GC.GetAllocatedBytesForCurrentThread();
            var verified = TruthReleaseVerification.Verify(directory, digest);
            var allocated = GC.GetAllocatedBytesForCurrentThread() - before;

            Assert.Equal(digest, verified.ReleaseDigest);
            Assert.True(allocated < report.Length / 2, $"Verification allocated {allocated:N0} bytes for a {report.Length:N0}-byte raw report.");
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    private static byte[] PairedRawReport(
        string? typeAddress = OfficialTypeAddress,
        string nameKey = "nk-a",
        string kind = "theorem",
        string module = "D5.S0.A",
        string sourcePath = "D5/S0/A.lean",
        string ignoredMetadata = "{}")
    {
        var typeField = typeAddress is null ? "" : $", \"type_sha256\": \"{typeAddress}\"";
        return Utf8.GetBytes($$"""
            {"modules": [{
              "declarations": [{"axioms": ["propext"], "include_in_statement": true, "kind": "{{kind}}",
                "name": "a", "name_key": "{{nameKey}}", "statement_id": "sha256:1111111111111111111111111111111111111111111111111111111111111111"{{typeField}}}],
              "imports": [], "information_templates": {{ignoredMetadata}}, "module": "{{module}}", "source_path": "{{sourcePath}}",
              "source_sha256": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            }], "schema": "stratalint-raw-lean-report-v4"}
            """);
    }

    private static (byte[] Graph, byte[] Export) PairedArtifacts(string graphPair = "matching")
    {
        var export = TruthExportJsonReader.Read(TruthExportBytes(Commit, Tree));
        var node = Assert.Single(export.Nodes);
        var declaration = Assert.Single(node.Declarations) with
        {
            Libraries = ["Init", "OmegaMath"], Axioms = ["propext"],
            StrictOmega = new("nk-a", "theorem", "sha256:" + new string('2', 64), OfficialTypeAddress,
                ["OmegaInit", "OmegaMath"], []),
        };
        var exportBytes = TruthExportJsonWriter.Write(export with { Nodes = [node with { Declarations = [declaration] }] });
        var graph = TruthGraphJsonReader.Read(MinimalTruthGraphBytes());
        var graphNode = new TruthGraphNode(node.RepoPath, null, "closed", null, 0)
        {
            StrictOmegaDeclarations = graphPair switch
            {
                "matching" => [declaration],
                "different" => [declaration with { StrictOmega = declaration.StrictOmega! with { Axioms = ["propext"] } }],
                "absent" => null,
                _ => throw new ArgumentOutOfRangeException(nameof(graphPair)),
            },
        };
        var graphBytes = TruthGraphJsonWriter.Write(graph with
        {
            Truth = graph.Truth with { Nodes = [graphNode], StateCounts = new(1, 0, 0, 0) },
        });
        return (graphBytes.ToArray(), exportBytes.ToArray());
    }

    [Fact]
    public void RejectsAnExpectedDigestThatDoesNotMatchTheBundle()
    {
        var (directory, _) = BuildBundle();
        try
        {
            var wrong = "sha256:" + new string('0', 64);
            Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, wrong));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void RejectsATamperedArtifact()
    {
        var (directory, digest) = BuildBundle();
        try
        {
            File.WriteAllText(Path.Combine(directory, "truth-export.v1.json"), "tampered", Utf8);
            Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void RejectsAMissingArtifactFile()
    {
        var (directory, digest) = BuildBundle();
        try
        {
            File.Delete(Path.Combine(directory, "truth-export.v1.json"));
            Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void RejectsATraversalFilenameInTheManifest()
    {
        var (directory, digest) = BuildBundle();
        try
        {
            var manifestPath = Path.Combine(directory, "release-manifest.v1.json");
            var evil = File.ReadAllText(manifestPath, Utf8)
                .Replace("\"truth-export.v1.json\"", "\"../escape.json\"", StringComparison.Ordinal);
            File.WriteAllText(manifestPath, evil, Utf8);
            Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void RejectsADuplicateArtifactFilename()
    {
        var (directory, digest) = BuildBundle();
        try
        {
            var manifestPath = Path.Combine(directory, "release-manifest.v1.json");
            var duplicate = File.ReadAllText(manifestPath, Utf8)
                .Replace("\"truth-graph.v1.json\"", "\"truth-export.v1.json\"", StringComparison.Ordinal);
            File.WriteAllText(manifestPath, duplicate, Utf8);
            Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void RejectsASymlinkedArtifact()
    {
        var (directory, digest) = BuildBundle();
        var externalDirectory = Directory.CreateTempSubdirectory("truthverify-ext").FullName;
        try
        {
            var external = Path.Combine(externalDirectory, "external.txt");
            File.WriteAllBytes(external, TruthExportBytes(Commit, Tree));
            var artifact = Path.Combine(directory, "truth-export.v1.json");
            File.Delete(artifact);
            File.CreateSymbolicLink(artifact, external);
            Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
            Directory.Delete(externalDirectory, recursive: true);
        }
    }

    private static void AssertCompositionRejected(Func<(string Directory, string Digest)> build)
    {
        var (directory, digest) = build();
        try
        {
            Assert.Throws<FormatException>(() => TruthReleaseVerification.Verify(directory, digest));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }
}
