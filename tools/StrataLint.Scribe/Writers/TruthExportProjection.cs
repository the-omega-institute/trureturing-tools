using System.Collections.Immutable;
using StrataLint.Engine;
using Trureturing.Truth;

namespace StrataLint.Scribe;

public static class TruthExportProjection
{
    public static TruthExportModel Project(
        ImmutableArray<FrozenNodeMaterial> closedNodes,
        ImmutableHashSet<RepoPath> frozenPaths,
        string sourceCommit,
        string sourceTree) => Project(closedNodes, frozenPaths.Contains, sourceCommit, sourceTree, null);

    internal static TruthExportModel Project(
        ImmutableArray<FrozenNodeMaterial> closedNodes,
        FrozenStateCatalog frozen,
        string sourceCommit,
        string sourceTree) => Project(closedNodes, frozen.Records.ContainsKey,
            sourceCommit, sourceTree, frozen.Records);

    private static TruthExportModel Project(
        ImmutableArray<FrozenNodeMaterial> closedNodes,
        Func<RepoPath, bool> isFrozen,
        string sourceCommit,
        string sourceTree,
        ImmutableDictionary<RepoPath, FrozenStateRecord>? frozenRecords)
    {
        var nodes = closedNodes
            .Select(node =>
            {
                var record = frozenRecords?.GetValueOrDefault(node.RepoPath);
                var annotations = record is not null && record.StatementId == node.StatementId
                    ? record.Declarations.ToDictionary(static declaration => declaration.Identity)
                    : null;
                return new TruthExportNode(
                    node.RepoPath.Value,
                    node.FrozenNodeId.Value,
                    node.AxiomClosure,
                    node.DeclarationStatementIds
                        .Select(declaration => ProjectDeclaration(declaration, annotations?.GetValueOrDefault(
                            (declaration.DeclarationNameKey, declaration.Kind))))
                        .ToImmutableArray(),
                    node.PrerequisiteFrozenNodeIds.Select(static id => id.Value).ToImmutableArray(),
                    isFrozen(node.RepoPath) ? "frozen" : "proven-not-yet-frozen");
            })
            .ToImmutableArray();

        return TruthExportModel.Create(nodes, sourceCommit, sourceTree);
    }

    private static TruthExportDeclaration ProjectDeclaration(FrozenDeclarationStatement declaration,
        FrozenDeclaration? annotation) => new(declaration.DeclarationNameKey, declaration.Kind, declaration.StatementId.Value)
    {
        Libraries = annotation?.Libraries,
        Axioms = annotation?.Axioms,
        OmegaOnlyAxiomFree = TruthExportValidation.IsOmegaOnlyAxiomFree(annotation?.Libraries, annotation?.Axioms)
            ? true : null,
    };
}
