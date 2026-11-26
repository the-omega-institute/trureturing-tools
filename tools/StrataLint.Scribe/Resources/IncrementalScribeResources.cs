using System.Collections.Immutable;
using System.Security.Cryptography;
using System.Text.Json;

namespace StrataLint.Scribe;

internal static class IncrementalScribeResources
{
    private sealed record Inputs(string Environment, string Pack, Dictionary<string, string> Scripts);

    internal static ScribeResourceScriptPackResult Write(
        string root, string output, ImmutableArray<string> paths,
        string cacheDirectory, string environment, string receipt)
    {
        var scripts = new Dictionary<string, string>(paths.Length, StringComparer.Ordinal);
        foreach (var path in paths)
        {
            using var source = File.OpenRead(Path.Combine(root, path));
            scripts.Add(path, Convert.ToHexStringLower(SHA256.HashData(source)));
        }

        Directory.CreateDirectory(cacheDirectory);
        var packPath = Path.Combine(cacheDirectory, "pack.zip");
        var inputsPath = Path.Combine(cacheDirectory, "inputs.json");
        var (previous, inputs) = ReadPrevious(packPath, inputsPath, environment);
        var changed = paths.Where(path => inputs is null
            || !inputs.Scripts.TryGetValue(path, out var hash) || hash != scripts[path]).ToImmutableArray();

        var result = ScribeResourceScriptPacker.Execute(root, changed, definitions =>
        {
            if (inputs is not null && changed.IsEmpty && inputs.Scripts.Count == paths.Length)
            {
                File.Copy(packPath, output, overwrite: true);
                return previous!.Manifest;
            }
            var updated = definitions.ToDictionary(definition => definition.SourcePath, StringComparer.Ordinal);
            return ScribeResourcePack.Write(output, paths.Select(path =>
                updated.TryGetValue(path, out var definition)
                    ? definition
                    : previous!.Read(path["Blueprint/".Length..^".scribe.cs".Length])));
        });
        if (result.Manifest is null) return result;

        var next = new Inputs(environment, result.Manifest.TotalSha256, scripts);
        var temp = Path.Combine(cacheDirectory, Path.GetRandomFileName());
        try
        {
            File.Copy(output, temp);
            File.Move(temp, packPath, overwrite: true);
            File.WriteAllBytes(temp, JsonSerializer.SerializeToUtf8Bytes(next));
            File.Move(temp, inputsPath, overwrite: true);
        }
        finally
        {
            File.Delete(temp);
        }
        File.WriteAllText(receipt, $"Scribe resources reused={paths.Length - changed.Length} built={changed.Length}\n");
        return result;
    }

    private static (ScribeResourcePack?, Inputs?) ReadPrevious(string pack, string inputs, string environment)
    {
        if (!File.Exists(inputs) || !File.Exists(pack)) return (null, null);
        var value = JsonSerializer.Deserialize<Inputs>(File.ReadAllBytes(inputs));
        if (value?.Environment != environment) return (null, null);
        var previous = ScribeResourcePack.Open(pack);
        return previous.Manifest.TotalSha256 == value.Pack ? (previous, value) : (null, null);
    }
}
