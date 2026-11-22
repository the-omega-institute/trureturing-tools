using System.Collections.Immutable;

namespace StrataLint.Engine;

public sealed record TowerComponentSyntax(
    string Id,
    string Kind,
    ImmutableArray<string> Members,
    ImmutableArray<string> JudgedBy,
    string Verification);

public sealed record TowerBootstrapSyntax(
    string Id,
    string Judge,
    string Reason,
    string Commit,
    int PullRequest,
    string Verification);

public sealed record TowerManifestSyntax(
    int SchemaVersion,
    ImmutableArray<TowerComponentSyntax> Components,
    TowerBootstrapSyntax Bootstrap);
