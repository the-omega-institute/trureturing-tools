using System.Collections.Immutable;
using System.Text;
using System.Text.Json;
using StrataLint.Engine;
using StrataLint.Runtime;

namespace StrataLint.Cli;

internal static class FrozenDependencyMeasurement
{
    internal delegate void Measure(string root, IReadOnlyDictionary<RepoPath, LeanFileReport> reports,
        IDictionary<RepoPath, FrozenStateRecord> records);

    internal static void Attach(string root, IReadOnlyDictionary<RepoPath, LeanFileReport> reports,
        IDictionary<RepoPath, FrozenStateRecord> records)
    {
        if (reports.Count == 0) return;
        var temporary = Directory.CreateTempSubdirectory("frozen-dependencies-");
        try
        {
            var request = Path.Combine(temporary.FullName, "request.json");
            var output = Path.Combine(temporary.FullName, "response.json");
            foreach (var batch in reports.Keys.Chunk(32))
            {
                var expected = batch.ToDictionary(LeanImportClosure.ModuleName, StringComparer.Ordinal);
                File.WriteAllBytes(request, JsonSerializer.SerializeToUtf8Bytes(expected.Keys));
                File.Delete(output);
                var result = BoundedProcessRunner.Run("/bin/bash",
                    ["tools/scripts/agent/frozen-dependencies.sh", request, output],
                    root, TimeSpan.FromMinutes(20), 1024 * 1024);
                if (result.ExitCode != 0)
                    throw new InvalidOperationException($"compiled declaration measurement exited {result.ExitCode}\n"
                        + Encoding.UTF8.GetString(result.StandardError) + Encoding.UTF8.GetString(result.StandardOutput));

                using var document = JsonDocument.Parse(File.ReadAllBytes(output));
                foreach (var module in document.RootElement.EnumerateArray())
                {
                    var name = module.GetProperty("module").GetString();
                    if (name is null || !expected.Remove(name, out var path))
                        throw new FormatException($"unexpected or duplicate measured module {name}");
                    var declarations = reports[path].Declarations.Where(static declaration => declaration.IncludeInStatement)
                        .ToDictionary(static declaration => (declaration.NameKey, declaration.Kind));
                    var measured = new Dictionary<(string, string), FrozenDeclaration>();
                    foreach (var row in module.GetProperty("declarations").EnumerateArray())
                    {
                        var key = RequiredString(row, "nameKey");
                        var kind = RequiredString(row, "kind");
                        var axioms = ReadSet(row, "axioms");
                        if (!declarations.Remove((key, kind), out var declaration)
                            || !StringComparer.Ordinal.Equals(declaration.StatementTypeAddress, RequiredString(row, "statementTypeAddress"))
                            || !axioms.SequenceEqual(declaration.Axioms.Order(StringComparer.Ordinal)))
                            throw new FormatException($"measured declaration does not match current report: {name}:{key}");
                        measured.Add((key, kind), new(key, kind, ReadSet(row, "libraries"),
                            axioms.Length == LeanAxiomFacts.StandardAxioms.Count ? null : axioms));
                    }
                    if (declarations.Count != 0)
                        throw new FormatException($"compiled declaration measurement is incomplete: {name}");
                    var record = records[path];
                    records[path] = record with
                    {
                        Declarations = record.Declarations.Select(declaration => measured[declaration.Identity]).ToImmutableArray(),
                    };
                }
                if (expected.Count != 0)
                    throw new FormatException("compiled declaration measurement is missing modules: " + string.Join(", ", expected.Keys));
            }
        }
        finally
        {
            temporary.Delete(recursive: true);
        }
    }

    private static string RequiredString(JsonElement row, string key) =>
        row.GetProperty(key).GetString() ?? throw new FormatException(key + " must be a string");

    private static ImmutableArray<string> ReadSet(JsonElement row, string key) =>
        row.GetProperty(key).EnumerateArray()
            .Select(item => item.GetString() ?? throw new FormatException(key + " must contain strings"))
            .Order(StringComparer.Ordinal).ToImmutableArray();
}
