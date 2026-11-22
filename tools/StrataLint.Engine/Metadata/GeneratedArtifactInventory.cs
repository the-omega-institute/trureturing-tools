using System.Collections.Immutable;

namespace StrataLint.Engine;

internal sealed record GeneratedArtifactIdentity(
    string Path,
    string Producer,
    string ArtifactId = "none");

// Identity and producer dispatch only. FILEMAP owns disposition and admission policy.
internal static class GeneratedArtifactInventory
{
    internal const string DocumentProducer = "ScribeEmitter";
    internal static GeneratedArtifactIdentity Values { get; } =
        new(RepositoryPathPolicy.ValuesProjectionPath, "ValuesEmitter", "A-VALUES");
    internal static GeneratedArtifactIdentity FileMap { get; } =
        new("Generated/FILEMAP.md", "FileMapEmitter", "A-FILEMAP");
    internal static GeneratedArtifactIdentity ScribeAttestation { get; } =
        new("tools/Generated/scribe-emissions.v1.json", DocumentProducer, "A-SCRIBE");

    internal static ImmutableArray<GeneratedArtifactIdentity> Create(
        IEnumerable<string> documentPaths)
    {
        ArgumentNullException.ThrowIfNull(documentPaths);
        var artifacts = documentPaths
            .Select(static path => new GeneratedArtifactIdentity(path, DocumentProducer))
            .Concat([Values, FileMap, ScribeAttestation])
            .OrderBy(static artifact => artifact.Path, StringComparer.Ordinal)
            .ToImmutableArray();
        if (artifacts.Select(static artifact => artifact.Path)
            .Distinct(StringComparer.Ordinal).Count() != artifacts.Length)
        {
            throw new InvalidOperationException("Generated artifact inventory contains duplicate paths.");
        }

        return artifacts;
    }
}
