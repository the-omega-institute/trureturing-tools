using System.Collections.Immutable;
using System.Text.Json;
using StrataLint.Engine;
using Trureturing.Truth;

namespace StrataLint.Cli;

/// Published projection inputs for the release named by the report merger.
internal sealed class TruthReleaseBase
{
    private static readonly JsonSerializerOptions Wire = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    private readonly Dictionary<string, TruthExportNode> exports;
    private readonly HashSet<string> rawModules;
    internal readonly Dictionary<string, TruthGraphNode> ReusedGraphNodes = new(StringComparer.Ordinal);
    private readonly Dictionary<string, TruthGraphNode> graph;
    internal string DirectoryPath { get; }
    internal IReadOnlySet<string> Changes { get; }

    private TruthReleaseBase(string directory, IReadOnlySet<string> changes)
    {
        DirectoryPath = directory;
        Changes = changes;
        rawModules = JsonSerializer.Deserialize<HashSet<string>>(File.ReadAllBytes(Path.Combine(directory, "reuse.json")))
            ?? throw new FormatException("reuse.json must be an array of module names.");
        using var export = Read(TruthReleaseBundleWriter.TruthExportFileName);
        exports = export.RootElement.GetProperty("nodes").Deserialize<TruthExportNode[]>(Wire)!
            .ToDictionary(static node => node.RepoPath, StringComparer.Ordinal);
        using var truthGraph = Read(TruthReleaseBundleWriter.TruthGraphFileName);
        graph = truthGraph.RootElement.GetProperty("truth").GetProperty("nodes").Deserialize<TruthGraphNode[]>(Wire)!
            .ToDictionary(static node => node.RepoPath, StringComparer.Ordinal);
    }

    private JsonDocument Read(string filename) => JsonDocument.Parse(File.ReadAllBytes(Path.Combine(DirectoryPath, filename)));

    internal static TruthReleaseBase? Open(string? directory, string producerCommit, string revision, IRepositoryGateway repository)
    {
        if (directory is null) return null;
        var publication = TruthReleasePublicationReader.Read(File.ReadAllBytes(
            Path.Combine(directory, TruthReleaseBundleWriter.PublicationFileName)));
        if (publication.ProducerCommit != producerCommit) return null;
        var changes = repository.ChangedPathsBetween(publication.SourceCommit, revision);
        return changes is null ? null : new TruthReleaseBase(directory, changes);
    }

    internal TruthExportNode? Reuse(RepoPath path, ImmutableArray<FrozenNodeId> prerequisites)
    {
        if (!rawModules.Contains(path.Value[..^5].Replace('/', '.')) || Changes.Contains(path.Value)
            || (FrozenStatePath.TryFromModulePath(path, out var statePath) && Changes.Contains(statePath.Value))
            || !exports.TryGetValue(path.Value, out var node)
            || !node.PrerequisiteFrozenNodeIds.SequenceEqual(prerequisites.Select(static id => id.Value)))
            return null;
        ReusedGraphNodes.Add(path.Value, graph[path.Value]);
        return node;
    }
}
