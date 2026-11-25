using System.Collections.Immutable;
using System.Text;
using System.Text.Json;
using StrataLint.Engine;
using StrataLint.Runtime;
using Trureturing.Truth;

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
        var updated = new Dictionary<RepoPath, FrozenStateRecord>();
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
                foreach (var (path, record) in ReadResponse(document.RootElement, expected, reports, records))
                    updated.Add(path, record);
            }
            foreach (var (path, record) in updated) records[path] = record;
        }
        finally
        {
            temporary.Delete(recursive: true);
        }
    }

    private static Dictionary<RepoPath, FrozenStateRecord> ReadResponse(JsonElement response,
        Dictionary<string, RepoPath> expected, IReadOnlyDictionary<RepoPath, LeanFileReport> reports,
        IDictionary<RepoPath, FrozenStateRecord> records)
    {
        var updated = new Dictionary<RepoPath, FrozenStateRecord>();
        foreach (var module in response.EnumerateArray())
        {
            RequireUniqueProperties(module);
            var name = module.GetProperty("module").GetString();
            if (name is null || !expected.Remove(name, out var path))
                throw new FormatException($"unexpected or duplicate measured module {name}");
            if (!module.TryGetProperty("strictEligible", out var eligibility)
                || eligibility.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new FormatException($"compiled declaration measurement requires strictEligible: {name}");
            var strictEligible = eligibility.GetBoolean();
            var declarations = reports[path].Declarations
                .ToDictionary(static declaration => (declaration.NameKey, declaration.Kind));
            var measured = new Dictionary<(string, string), FrozenDeclaration>();
            foreach (var row in module.GetProperty("declarations").EnumerateArray())
            {
                RequireUniqueProperties(row);
                var key = RequiredString(row, "nameKey");
                var kind = RequiredString(row, "kind");
                var included = row.GetProperty("includeInStatement").GetBoolean();
                var axioms = ReadSet(row, "axioms");
                if (!declarations.Remove((key, kind), out var declaration)
                    || declaration.IncludeInStatement != included
                    || !StringComparer.Ordinal.Equals(declaration.StatementTypeAddress, RequiredString(row, "statementTypeAddress"))
                    || !axioms.SequenceEqual(declaration.Axioms.Order(StringComparer.Ordinal)))
                    throw new FormatException($"measured declaration does not match current report: {name}:{key}");

                var hasStrict = row.TryGetProperty("strict_omega", out var strictElement);
                var officialDerivedOnly = row.TryGetProperty("officialDerivedOnly", out var derived);
                if (officialDerivedOnly && (derived.ValueKind != JsonValueKind.True || !strictEligible || !included || hasStrict))
                    throw new FormatException($"invalid officialDerivedOnly measurement: {name}:{key}");
                if (hasStrict != (strictEligible && included && !officialDerivedOnly))
                    throw new FormatException($"strict_omega eligibility does not match measured declaration: {name}:{key}");
                if (!included) continue;
                var strict = hasStrict ? StrictOmegaMeasurement.Read(strictElement) : null;
                measured.Add((key, kind), new(key, kind, ReadSet(row, "libraries"), axioms, strict));
            }
            if (declarations.Count != 0)
                throw new FormatException($"compiled declaration measurement is incomplete: {name}");
            var record = records[path];
            var paired = record with
            {
                Declarations = record.Declarations.Select(declaration => measured[declaration.Identity]).ToImmutableArray(),
            };
            FrozenStateRecord.ValidateDeclarations(paired.Declarations);
            paired.ValidatePairedStatements(reports[path]);
            updated.Add(path, paired);
        }
        if (expected.Count != 0)
            throw new FormatException("compiled declaration measurement is missing modules: " + string.Join(", ", expected.Keys));
        return updated;
    }

    private static void RequireUniqueProperties(JsonElement element)
    {
        var names = new HashSet<string>(StringComparer.Ordinal);
        if (element.ValueKind != JsonValueKind.Object || element.EnumerateObject().Any(property => !names.Add(property.Name)))
            throw new FormatException("compiled declaration measurement requires objects with unique keys");
    }

    private static string RequiredString(JsonElement row, string key) =>
        row.GetProperty(key).GetString() ?? throw new FormatException(key + " must be a string");

    private static ImmutableArray<string> ReadSet(JsonElement row, string key) =>
        row.GetProperty(key).EnumerateArray()
            .Select(item => item.GetString() ?? throw new FormatException(key + " must contain strings"))
            .Order(StringComparer.Ordinal).ToImmutableArray();
}
