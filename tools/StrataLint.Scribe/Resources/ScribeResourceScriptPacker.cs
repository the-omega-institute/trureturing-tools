using System.Collections.Immutable;

namespace StrataLint.Scribe;

public sealed record ScribeResourceScriptPackResult(
    ScribeResourcePackManifest? Manifest,
    ImmutableArray<ScribeScriptFailure> Failures);

/// <summary>Executes every Blueprint script and writes its canonical resource definition.</summary>
public static class ScribeResourceScriptPacker
{
    public static ScribeResourceScriptPackResult Write(string repositoryRoot, string outputPath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(repositoryRoot);
        ArgumentException.ThrowIfNullOrWhiteSpace(outputPath);
        var root = Path.GetFullPath(repositoryRoot);
        var paths = Directory.EnumerateFiles(Path.Combine(root, "Blueprint"), "*.scribe.cs", SearchOption.AllDirectories)
            .Select(path => Path.GetRelativePath(root, path).Replace('\\', '/'))
            .Order(StringComparer.Ordinal).ToImmutableArray();
        var cache = Environment.GetEnvironmentVariable("TRUTH_RELEASE_SCRIBE_CACHE");
        var environment = Environment.GetEnvironmentVariable("TRUTH_RELEASE_SCRIBE_INPUT");
        var receipt = Environment.GetEnvironmentVariable("TRUTH_RELEASE_SCRIBE_RECEIPT");
        if (!string.IsNullOrEmpty(cache) && !string.IsNullOrEmpty(environment) && !string.IsNullOrEmpty(receipt))
            return IncrementalScribeResources.Write(root, outputPath, paths, cache, environment, receipt);
        return Execute(root, paths, definitions => ScribeResourcePack.Write(outputPath, definitions));
    }

    internal static ScribeResourceScriptPackResult Execute(string root, ImmutableArray<string> paths,
        Func<IEnumerable<DocumentDefinition>, ScribeResourcePackManifest> write) =>
        StatementProjectionFixtureLoader.WithFreshRepositoryRoot(root, () =>
        {
            var results = ScribeScriptHost.ExecuteBatch(root, paths);
            var failures = results.Where(result => !result.IsSuccess).Select(result =>
            {
                var failure = result.Failure!;
                return failure.RelativePath == result.RelativePath ? failure : failure with
                {
                    RelativePath = result.RelativePath, Message = failure.ToString(),
                };
            }).ToImmutableArray();
            if (!failures.IsEmpty)
            {
                return new ScribeResourceScriptPackResult(null, failures);
            }
            var manifest = write(results.Select(result => result.Definition!));
            return new ScribeResourceScriptPackResult(manifest, failures);
        });
}
