using System.Collections.Immutable;
using System.Text;
using Xunit;

namespace Trureturing.Truth.Tests;

public sealed class TruthGraphJsonTests
{
    private const string Target = "D5/S0/Carrier/Target";
    private const string Source = "D5/S0/Carrier/Source";
    private const string TargetModule = "D5.S0.Carrier.Target";
    private const string TargetLean = Target + ".lean";
    private const string TargetDocument = "Blueprint/" + Target + ".md";
    private const string SourceDocument = "Blueprint/" + Source + ".md";
    private const string Declaration = Target + ".anchor";

    private static readonly TruthGraphExportModel PublishedGraph = new(
        TruthGraphExportModel.Dialect,
        1,
        new TruthGraphProvenance(
            "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        {
            TruthRootSha256 = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        },
        new TruthGraphSection(
            [new(Source + ".lean", Source, "open", "D5.S0.Carrier.Source", 1),
                new(TargetLean, Target, "closed", TargetModule, 0),
                new("Meta/notes.md", null, "semantic", null, 0)],
            [new(TargetLean, Source + ".lean")],
            [new(Source + ".lean", "D5.S0.Carrier.Absent")],
            new(1, 1, 0, 1)),
        new DocumentGraphSection(
            [new(SourceDocument, Source, "receipt-free"), new(TargetDocument, Target, "receipt-bound")],
            [new(TargetDocument, Target, "first", "lean", Declaration, "lean-derived"),
                new(TargetDocument, Target, "second", "lean", Declaration, "lean-derived")],
            [new(TargetDocument, SourceDocument)],
            [new(SourceDocument, TargetDocument)]),
        new TruthGraphJoinsSection(
            [new(SourceDocument, Source, null, Declaration, TargetLean),
                new(TargetDocument, Target, "first", Declaration, TargetLean),
                new(TargetDocument, Target, "second", Declaration, TargetLean)]),
        ["digestion"]);

    [Fact]
    public void StrictReaderRoundTripsEveryPublishedGraphField()
    {
        var bytes = TruthGraphJsonWriter.Write(PublishedGraph);
        var actual = TruthGraphJsonReader.Read(bytes.AsSpan());

        Assert.Equal(PublishedGraph.Schema, actual.Schema);
        Assert.Equal(PublishedGraph.SchemaVersion, actual.SchemaVersion);
        Assert.Equal(PublishedGraph.Provenance, actual.Provenance);
        Assert.Equal(PublishedGraph.Truth.Nodes.ToArray(), actual.Truth.Nodes.ToArray());
        Assert.Equal(PublishedGraph.Truth.Edges.ToArray(), actual.Truth.Edges.ToArray());
        Assert.Equal(PublishedGraph.Truth.OpenBlockers.ToArray(), actual.Truth.OpenBlockers.ToArray());
        Assert.Equal(PublishedGraph.Truth.StateCounts, actual.Truth.StateCounts);
        Assert.Equal(PublishedGraph.Documents.Nodes.ToArray(), actual.Documents.Nodes.ToArray());
        Assert.Equal(PublishedGraph.Documents.DescribeNodes.ToArray(), actual.Documents.DescribeNodes.ToArray());
        Assert.Equal(PublishedGraph.Documents.DependencyEdges.ToArray(), actual.Documents.DependencyEdges.ToArray());
        Assert.Equal(PublishedGraph.Documents.NarrativeReferenceEdges.ToArray(), actual.Documents.NarrativeReferenceEdges.ToArray());
        Assert.Equal(PublishedGraph.Joins.TruthAnchors.ToArray(), actual.Joins.TruthAnchors.ToArray());
        Assert.Equal(PublishedGraph.DeferredLayers.ToArray(), actual.DeferredLayers.ToArray());
        Assert.Equal(bytes.ToArray(), TruthGraphJsonWriter.Write(actual).ToArray());
    }

    [Fact]
    public void StrictReaderRejectsInconsistentDescribeAndAnchorIdentities()
    {
        var anchors = PublishedGraph.Joins.TruthAnchors;
        var danglingAnchor = PublishedGraph with
        {
            Joins = new(anchors.SetItem(2, anchors[2] with { DescribeId = "missing" })),
        };
        var misboundDocument = PublishedGraph with
        {
            Documents = PublishedGraph.Documents with
            {
                DescribeNodes = PublishedGraph.Documents.DescribeNodes
                    .Select(static node => node with { DocumentGid = Source }).ToImmutableArray(),
            },
        };
        var retargetedDeclaration = PublishedGraph with
        {
            Documents = PublishedGraph.Documents with
            {
                DescribeNodes = PublishedGraph.Documents.DescribeNodes
                    .Select(static node => node with { LeanDeclarationGid = Target + ".other" }).ToImmutableArray(),
            },
        };

        Assert.Throws<FormatException>(() => TruthGraphJsonReader.Read(TruthGraphJsonWriter.Write(danglingAnchor).AsSpan()));
        Assert.Throws<FormatException>(() => TruthGraphJsonReader.Read(TruthGraphJsonWriter.Write(misboundDocument).AsSpan()));
        Assert.Throws<FormatException>(() => TruthGraphJsonReader.Read(TruthGraphJsonWriter.Write(retargetedDeclaration).AsSpan()));
    }

    [Theory]
    [InlineData("{}\n")]
    [InlineData("{\"schema\":\"stratalint.truth-graph.v1\",\"schema_version\":1,\"provenance\":{},\"truth\":{},\"extra\":true}\n")]
    [InlineData("{\"schema\":\"wrong\",\"schema_version\":1,\"provenance\":{},\"truth\":{}}\n")]
    public void StrictReaderRejectsMalformedOrUnknownFields(string json) =>
        Assert.Throws<FormatException>(() => TruthGraphJsonReader.Read(Encoding.UTF8.GetBytes(json)));
}
