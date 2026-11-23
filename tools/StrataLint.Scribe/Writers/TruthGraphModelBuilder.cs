using System.Collections.Immutable;
using StrataLint.Engine;
using Trureturing.Truth;

namespace StrataLint.Scribe;

public sealed record DocumentGraphDocument(
    string RepoPath,
    ScribeDocument Document,
    string Receipt);

/// Builds <see cref="TruthGraphExportModel"/> from the Scribe-owned truth projection.
/// The model record lives in Trureturing.Truth (zero StrataLint dependency); this construction
/// step reads TruthDagProjection and therefore stays in Scribe, which references Engine.
public static class TruthGraphModelBuilder
{
    public static TruthGraphExportModel Create(
        TruthDagProjection dag,
        TruthGraphProvenance provenance,
        IEnumerable<DocumentDefinition> definitions,
        DeclarationCatalog catalog,
        out int unresolvedReferences)
    {
        ArgumentNullException.ThrowIfNull(dag);
        ArgumentNullException.ThrowIfNull(provenance);
        var formalPaths = dag.Nodes.Select(static node => node.RepoPath.Value)
            .ToHashSet(StringComparer.Ordinal);
        var material = definitions.Select(definition => new DocumentGraphDocument(
            definition.RelativePath.Value, definition.Document, "receipt-free")).ToImmutableArray();
        var graph = DocumentGraphAssembler.Assemble(material.Select(item => item.Document), catalog,
            validateTargets: false);
        var documentProjection = DocumentGraphExportProjection.Create(material, graph, catalog, formalPaths,
            out unresolvedReferences);
        return Create(dag, provenance, documentProjection);
    }

    public static TruthGraphExportModel Create(
        TruthDagProjection dag,
        TruthGraphProvenance provenance,
        DocumentGraphExportProjection? documentProjection = null)
    {
        ArgumentNullException.ThrowIfNull(dag);
        ArgumentNullException.ThrowIfNull(provenance);
        var nodes = dag.Nodes
            .Select(node => new TruthGraphNode(
                node.RepoPath.Value,
                node.Gid?.Value,
                StateName(node.State),
                node.ModuleName,
                dag.Depth(node.RepoPath)))
            .ToImmutableArray();
        var edges = dag.Edges
            .Select(static edge => new TruthGraphEdge(edge.Dependency.Value, edge.Dependent.Value))
            .ToImmutableArray();
        var blockers = dag.OpenBlockers
            .Select(static blocker => new TruthGraphOpenBlocker(
                blocker.Dependent.Value,
                blocker.DependencyModule))
            .ToImmutableArray();
        var counts = new TruthGraphStateCounts(
            nodes.Count(static node => node.State == "closed"),
            nodes.Count(static node => node.State == "open"),
            nodes.Count(static node => node.State == "tail"),
            nodes.Count(static node => node.State == "semantic"));
        var projection = documentProjection ?? DocumentGraphExportProjection.Empty;
        return new TruthGraphExportModel(
            TruthGraphExportModel.Dialect,
            1,
            provenance with
            {
                TruthRootSha256 = dag.RootSha256,
                DependencyGranularity = "module-import",
            },
            new TruthGraphSection(nodes, edges, blockers, counts),
            projection.Documents,
            projection.Joins,
            ["digestion"]);
    }

    private static string StateName(TruthState state) => state switch
    {
        TruthState.Closed => "closed",
        TruthState.Open => "open",
        TruthState.Tail => "tail",
        TruthState.Semantic => "semantic",
        _ => throw new InvalidOperationException($"Unknown truth state {state}."),
    };
}

/// Builds <see cref="DocumentGraphExportProjection"/> from Scribe/Engine document graph
/// material. The projection record lives in Trureturing.Truth; this assembly step depends on the
/// document AST and declaration catalog and therefore stays in Scribe.
public static class DocumentGraphExportProjectionExtensions
{
    extension(DocumentGraphExportProjection)
    {
        public static DocumentGraphExportProjection Create(
            IEnumerable<DocumentGraphDocument> documents,
            DocumentGraph graph,
            DeclarationCatalog catalog,
            IReadOnlySet<string> formalTruthRepoPaths,
            out int unresolvedReferences)
        {
            ArgumentNullException.ThrowIfNull(documents);
            ArgumentNullException.ThrowIfNull(graph);
            ArgumentNullException.ThrowIfNull(catalog);
            ArgumentNullException.ThrowIfNull(formalTruthRepoPaths);
            unresolvedReferences = 0;
            var material = documents.ToImmutableArray();
            var byGid = material.ToDictionary(
                static item => item.Document.Header.Gid.Value,
                StringComparer.Ordinal);
            if (material.Select(static item => item.RepoPath).Distinct(StringComparer.Ordinal).Count() != material.Length)
            {
                throw new InvalidOperationException("Document graph export contains duplicate repository paths.");
            }
            if (material.Any(static item => item.Receipt is not ("receipt-free" or "receipt-bound")))
            {
                throw new InvalidOperationException("Document graph export contains an unsupported receipt category.");
            }

            var nodes = material
                .OrderBy(static item => item.RepoPath, StringComparer.Ordinal)
                .Select(static item => new DocumentGraphNode(
                    item.RepoPath,
                    item.Document.Header.Gid.Value,
                    item.Receipt))
                .ToImmutableArray();
            var dependencies = ImmutableArray.CreateBuilder<DocumentDependencyEdge>();
            var narratives = ImmutableArray.CreateBuilder<DocumentNarrativeReferenceEdge>();
            var anchors = ImmutableArray.CreateBuilder<TruthAnchorJoin>();
            var describeNodes = material.SelectMany(source => EnumerateDescribes(source.Document.Content)
                    .Select(describe => ProjectDescribe(source, describe, catalog)))
                .OrderBy(static node => node.RepoPath, StringComparer.Ordinal)
                .ThenBy(static node => node.DescribeId, StringComparer.Ordinal)
                .ToImmutableArray();
            var describes = describeNodes.ToDictionary(node => (node.DocumentGid, node.DescribeId));
            foreach (var source in material)
            {
                foreach (var edge in graph.For(source.Document))
                {
                    switch (edge)
                    {
                        case DocumentEdge.Dependency dependency:
                            if (byGid.TryGetValue(dependency.Target.Value, out var target))
                                dependencies.Add(new DocumentDependencyEdge(target.RepoPath, source.RepoPath));
                            else
                                unresolvedReferences++;
                            break;
                        case DocumentEdge.NarrativeReference narrative:
                            var targetGid = narrative.Target switch
                            {
                                NarrativeTarget.Document document => document.DocumentGid.Value,
                                NarrativeTarget.Describe block => block.DocumentGid.Value,
                                _ => throw new InvalidOperationException("Unknown narrative target."),
                            };
                            var fragment = narrative.Target is NarrativeTarget.Describe describe
                                ? $"#describe/{describe.DescribeId.Value}"
                                : string.Empty;
                            if (byGid.TryGetValue(targetGid, out var destination)
                                && (narrative.Target is not NarrativeTarget.Describe item
                                    || describes.ContainsKey((targetGid, item.DescribeId.Value))))
                                narratives.Add(new DocumentNarrativeReferenceEdge(source.RepoPath, destination.RepoPath + fragment));
                            else
                                unresolvedReferences++;
                            break;
                        case DocumentEdge.TruthAnchor anchor:
                            var formalPath = anchor.Target.Reference.Path.Value;
                            if (!formalTruthRepoPaths.Contains(formalPath)
                                || !Resolves(catalog, anchor.Target.Value)
                                || (anchor.DescribeId is { } id
                                    && (!describes.TryGetValue((source.Document.Header.Gid.Value, id.Value), out var node)
                                        || node.LeanDeclarationGid != anchor.Target.Value)))
                            {
                                unresolvedReferences++;
                                break;
                            }
                            anchors.Add(new TruthAnchorJoin(
                                source.RepoPath,
                                source.Document.Header.Gid.Value,
                                anchor.DescribeId?.Value,
                                anchor.Target.Value,
                                formalPath));
                            break;
                    }
                }
            }

            return new DocumentGraphExportProjection(
                new DocumentGraphSection(
                    nodes,
                    describeNodes,
                    dependencies.OrderBy(static edge => edge.Dependency, StringComparer.Ordinal)
                        .ThenBy(static edge => edge.Dependent, StringComparer.Ordinal).ToImmutableArray(),
                    narratives.OrderBy(static edge => edge.Source, StringComparer.Ordinal)
                        .ThenBy(static edge => edge.Target, StringComparer.Ordinal).ToImmutableArray()),
                new TruthGraphJoinsSection(
                    anchors.OrderBy(static anchor => anchor.DocumentRepoPath, StringComparer.Ordinal)
                        .ThenBy(static anchor => anchor.DescribeId, StringComparer.Ordinal)
                        .ThenBy(static anchor => anchor.LeanDeclarationGid, StringComparer.Ordinal)
                        .ToImmutableArray()));
        }
    }

    // Only resolved references can form a join in the v1 graph.
    private static bool Resolves(DeclarationCatalog catalog, string gid)
    {
        try { _ = catalog.Resolve(DeclarationHandle.Create(gid)); return true; }
        catch (InvalidOperationException) { return false; }
    }

    private static DescribeGraphNode ProjectDescribe(DocumentGraphDocument source, DocumentBlock.Describe describe,
        DeclarationCatalog catalog)
    {
        var target = describe.Statement is DescribeStatement.LeanDeclaration lean && Resolves(catalog, lean.Value.Value)
            ? lean.Value.Value : null;
        string kind;
        try { kind = DescribeVocabulary.CanonicalName(catalog.ResolveKind(describe)); }
        catch (InvalidOperationException) { kind = "unresolved"; }
        return new DescribeGraphNode(source.RepoPath, source.Document.Header.Gid.Value, describe.Id.Value,
            kind, target, describe.FormulaProvenance == StatementFormulaProvenance.LeanDerived ? "lean-derived" : "hand-authored");
    }

    private static IEnumerable<DocumentBlock.Describe> EnumerateDescribes(BlockSequence blocks)
    {
        foreach (var block in blocks.Items)
        {
            if (block is DocumentBlock.Describe describe)
            {
                yield return describe;
                foreach (var nested in EnumerateDescribes(describe.Content)) yield return nested;
            }
            else if (block is DocumentBlock.Section section)
            {
                foreach (var nested in EnumerateDescribes(section.Content)) yield return nested;
            }
        }
    }
}
