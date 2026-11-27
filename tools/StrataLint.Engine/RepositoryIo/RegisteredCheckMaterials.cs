using System.Text.Json;

namespace StrataLint.Engine;

// Engine predicates consume the explicit material declaration.
// This projection reads no program, report or inventory fields and invents no inputs.
internal static class RegisteredCheckMaterials
{
    internal const string ManifestPath = "Meta/ci-checks.json";
    internal const string ReleaseManifestPath = "tools/ci/ci-checks.json";
    private sealed record Manifest(string Schema, MaterialDeclaration[] Checks);
    private sealed record MaterialDeclaration(string Id, string[] Materials, string[] MaterialExcludes,
        string[]? DirectoryCapacityExemptRoots = null);
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        AllowDuplicateProperties = false,
        RespectRequiredConstructorParameters = true,
    };

    internal static bool IsManifestPath(string path) => path is ManifestPath or ReleaseManifestPath;

    internal static string PathIn(RepositorySnapshot snapshot) =>
        !snapshot.TryGetFile(ManifestPath, out _) && snapshot.TryGetFile(ReleaseManifestPath, out _)
            ? ReleaseManifestPath : ManifestPath;

    internal static string[] Read(RepositorySnapshot snapshot, string id)
    {
        var check = Declaration(snapshot, id);
        return EngineeringProjectRegistry.ExpandInputs(snapshot.Files.Keys.Select(path => path.Value),
            check.Materials, check.MaterialExcludes, id);
    }

    internal static string[] DirectoryCapacityRoots(RepositorySnapshot snapshot)
    {
        var path = PathIn(snapshot);
        return snapshot.TryGetFile(path, out var file) ? DirectoryCapacityRoots(file.Text, path) : [];
    }

    internal static string[] DirectoryCapacityRoots(string text, string path)
    {
        var roots = Parse(text, path, "SL-003").DirectoryCapacityExemptRoots ?? [];
        if (roots.Any(root => !RepoPath.TryCreate(root, out _) || root.IndexOfAny(['*', '?', '[', ']']) >= 0)
            || roots.Distinct(StringComparer.Ordinal).Count() != roots.Length)
            throw new InvalidDataException($"invalid directory_capacity_exempt_roots in {path}: expected distinct literal repository directories");
        return roots.Select(root => root + "/").ToArray();
    }

    private static MaterialDeclaration Declaration(RepositorySnapshot snapshot, string id)
    {
        var path = PathIn(snapshot);
        if (!snapshot.TryGetFile(path, out var file))
            throw new InvalidDataException($"missing check material registration: {path}: {id}");
        return Parse(file.Text, path, id);
    }

    private static MaterialDeclaration Parse(string text, string path, string id)
    {
        try
        {
            var manifest = JsonSerializer.Deserialize<Manifest>(text, Options);
            if (manifest is null || manifest.Schema != "ci-check-input-registration-v2"
                || manifest.Checks is null || manifest.Checks.Any(check => check is null || string.IsNullOrWhiteSpace(check.Id))
                || manifest.Checks.Select(check => check.Id).Distinct(StringComparer.Ordinal).Count() != manifest.Checks.Length)
                throw new InvalidDataException($"invalid check material registration: {path}: {id}");
            var check = manifest.Checks.SingleOrDefault(check => check.Id == id)
                ?? throw new InvalidDataException($"missing check material registration: {path}: {id}");
            EngineeringProjectRegistry.ValidateMaterials(check.Materials, check.MaterialExcludes, id);
            return check;
        }
        catch (Exception exception) when (exception is JsonException or FormatException or InvalidDataException)
        {
            throw new InvalidDataException($"invalid check material registration: {path}: {id}: {exception.Message}", exception);
        }
    }
}
