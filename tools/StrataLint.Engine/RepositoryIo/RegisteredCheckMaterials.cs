using System.Text.Json;

namespace StrataLint.Engine;

// Engine predicates consume the explicit material declaration.
// This projection reads no program, report or inventory fields and invents no inputs.
internal static class RegisteredCheckMaterials
{
    internal const string ManifestPath = "Meta/ci-checks.json";
    private const string ReleaseManifestPath = "tools/ci/ci-checks.json";
    private sealed record Manifest(string Schema, MaterialDeclaration[] Checks);
    private sealed record MaterialDeclaration(string Id, string[] Materials, string[] MaterialExcludes);
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        AllowDuplicateProperties = false,
        RespectRequiredConstructorParameters = true,
    };

    internal static string PathIn(RepositorySnapshot snapshot) =>
        !snapshot.TryGetFile(ManifestPath, out _) && snapshot.TryGetFile(ReleaseManifestPath, out _)
            ? ReleaseManifestPath : ManifestPath;

    internal static string[] Read(RepositorySnapshot snapshot, string id)
    {
        var path = PathIn(snapshot);
        if (!snapshot.TryGetFile(path, out var file))
            throw new InvalidDataException($"missing check material registration: {path}: {id}");
        try
        {
            var manifest = JsonSerializer.Deserialize<Manifest>(file.Text, Options);
            if (manifest is null || manifest.Schema != "ci-check-input-registration-v2"
                || manifest.Checks is null || manifest.Checks.Any(check => check is null || string.IsNullOrWhiteSpace(check.Id))
                || manifest.Checks.Select(check => check.Id).Distinct(StringComparer.Ordinal).Count() != manifest.Checks.Length)
                throw new InvalidDataException($"invalid check material registration: {path}: {id}");
            var check = manifest.Checks.SingleOrDefault(check => check.Id == id)
                ?? throw new InvalidDataException($"missing check material registration: {path}: {id}");
            EngineeringProjectRegistry.ValidateMaterials(check.Materials, check.MaterialExcludes, id);
            return EngineeringProjectRegistry.ExpandInputs(snapshot.Files.Keys.Select(path => path.Value),
                check.Materials, check.MaterialExcludes, id);
        }
        catch (Exception exception) when (exception is JsonException or FormatException or InvalidDataException)
        {
            throw new InvalidDataException($"invalid check material registration: {path}: {id}: {exception.Message}", exception);
        }
    }
}
