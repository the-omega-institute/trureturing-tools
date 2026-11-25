using System.Collections.Immutable;
using System.Text.Json.Serialization;

namespace Trureturing.Truth;

public sealed record TruthGraphProvenance(
    string SnapshotContentDigest,
    string LeanReportDigest)
{
    public string TruthRootSha256 { get; init; } = string.Empty;

    public string DependencyGranularity { get; init; } = "module-import";
}

public sealed record TruthGraphNode(
    [property: JsonPropertyName("repo_path")] string RepoPath,
    [property: JsonPropertyName("gid")] string? Gid,
    [property: JsonPropertyName("state")] string State,
    [property: JsonPropertyName("module_name")] string? ModuleName,
    [property: JsonPropertyName("depth")] int Depth)
{
    [JsonPropertyName("omega_only_axiom_free_declarations"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public ImmutableArray<TruthGraphDeclaration>? OmegaOnlyAxiomFreeDeclarations { get; init; }

    [JsonPropertyName("strict_omega_declarations"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public ImmutableArray<TruthExportDeclaration>? StrictOmegaDeclarations { get; init; }
}

/// A positive declaration marker anchored to the containing truth node and a truth-export statement.
public sealed record TruthGraphDeclaration(
    [property: JsonPropertyName("declaration_name_key")] string DeclarationNameKey,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("statement_id")] string StatementId);

public sealed record TruthGraphEdge(string Dependency, string Dependent);

public sealed record TruthGraphOpenBlocker(string Dependent, string DependencyModule);

public sealed record TruthGraphStateCounts(int Closed, int Open, int Tail, int Semantic)
{
    public int Total => Closed + Open + Tail + Semantic;
}

public sealed record TruthGraphSection(
    ImmutableArray<TruthGraphNode> Nodes,
    ImmutableArray<TruthGraphEdge> Edges,
    ImmutableArray<TruthGraphOpenBlocker> OpenBlockers,
    TruthGraphStateCounts StateCounts);

public sealed record DocumentGraphNode(string RepoPath, string Gid, string Receipt);

public sealed record DocumentDependencyEdge(string Dependency, string Dependent);

public sealed record DocumentNarrativeReferenceEdge(string Source, string Target);

public sealed record DescribeGraphNode(
    string RepoPath,
    string DocumentGid,
    string DescribeId,
    string Kind,
    string? LeanDeclarationGid,
    string FormulaProvenance);

public sealed record DocumentGraphSection(
    ImmutableArray<DocumentGraphNode> Nodes,
    ImmutableArray<DescribeGraphNode> DescribeNodes,
    ImmutableArray<DocumentDependencyEdge> DependencyEdges,
    ImmutableArray<DocumentNarrativeReferenceEdge> NarrativeReferenceEdges);

public sealed record TruthAnchorJoin(
    string DocumentRepoPath,
    string DocumentGid,
    string? DescribeId,
    string LeanDeclarationGid,
    string FormalTruthRepoPath);

public sealed record TruthGraphJoinsSection(ImmutableArray<TruthAnchorJoin> TruthAnchors);

public sealed record DocumentGraphExportProjection(
    DocumentGraphSection Documents,
    TruthGraphJoinsSection Joins)
{
    public static DocumentGraphExportProjection Empty { get; } = new(
        new DocumentGraphSection([], [], [], []),
        new TruthGraphJoinsSection([]));
}

public sealed record TruthGraphExportModel(
    string Schema,
    int SchemaVersion,
    TruthGraphProvenance Provenance,
    TruthGraphSection Truth,
    DocumentGraphSection Documents,
    TruthGraphJoinsSection Joins,
    ImmutableArray<string> DeferredLayers)
{
    public const string Dialect = "stratalint.truth-graph.v1";
}
