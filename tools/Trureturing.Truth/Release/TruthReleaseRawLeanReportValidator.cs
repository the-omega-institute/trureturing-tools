using System.Security.Cryptography;
using System.Text.Json;

namespace Trureturing.Truth;

internal static class TruthReleaseRawLeanReportValidator
{
    internal static string HashAndValidate(Stream report, TruthExportModel truthExport)
    {
        var expected = new Dictionary<string, Dictionary<(string NameKey, string Kind), string>>(StringComparer.Ordinal);
        var annotatedIdentities = new HashSet<(string NameKey, string Kind)>();
        foreach (var node in truthExport.Nodes)
        {
            foreach (var declaration in node.Declarations)
            {
                if (declaration.StrictOmega is not { } strict)
                    continue;
                var identity = (declaration.DeclarationNameKey, declaration.Kind);
                if (!expected.TryGetValue(node.RepoPath, out var declarations))
                    expected.Add(node.RepoPath, declarations = []);
                if (!declarations.TryAdd(identity, strict.OfficialStatementTypeAddress))
                    throw new FormatException($"Truth release has duplicate strict Omega declarations for '{node.RepoPath}'.");
                annotatedIdentities.Add(identity);
            }
        }

        // Historical bundles without pairs bind the report through its digest alone.
        if (expected.Count == 0)
            return Convert.ToHexStringLower(SHA256.HashData(report));

        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var reader = new ReportReader(report, hash);
        try
        {
            reader.ReadToken(JsonTokenType.StartObject);
            var seenPaths = new HashSet<string>(StringComparer.Ordinal);
            var sawModules = false;
            string? schema = null;
            while (reader.ReadProperty())
            {
                if (reader.ValueIs("schema"u8))
                    reader.ReadStringOnce(ref schema);
                else if (reader.ValueIs("modules"u8))
                {
                    if (sawModules)
                        throw InvalidReport();
                    sawModules = true;
                    reader.ReadToken(JsonTokenType.StartArray);
                    while (reader.ReadObjectItem())
                        ReadModule(ref reader, expected, annotatedIdentities, seenPaths);
                }
                else
                    reader.SkipValue();
            }

            if (reader.Read())
                throw InvalidReport();
            if (!string.Equals(schema, "stratalint-raw-lean-report-v4", StringComparison.Ordinal))
                throw new FormatException("Truth release strict Omega measurements require raw_lean_report schema stratalint-raw-lean-report-v4.");

            foreach (var path in expected.Keys)
            {
                if (!seenPaths.Contains(path))
                    throw new FormatException($"Truth release raw_lean_report is missing the strict Omega module for '{path}'.");
            }
        }
        catch (JsonException exception)
        {
            throw new FormatException("Truth release raw_lean_report is not valid JSON.", exception);
        }

        return Convert.ToHexStringLower(hash.GetHashAndReset());
    }

    private static void ReadModule(
        ref ReportReader reader,
        Dictionary<string, Dictionary<(string NameKey, string Kind), string>> expected,
        HashSet<(string NameKey, string Kind)> annotatedIdentities,
        HashSet<string> seenPaths)
    {
        string? path = null;
        string? module = null;
        // Canonical v4 places declarations before module and source_path. Keep only identities
        // that could match an annotation, then bind them to this module once its path is known.
        Dictionary<(string NameKey, string Kind), string?>? addresses = null;
        while (reader.ReadProperty())
        {
            if (reader.ValueIs("source_path"u8))
                reader.ReadStringOnce(ref path);
            else if (reader.ValueIs("module"u8))
                reader.ReadStringOnce(ref module);
            else if (reader.ValueIs("declarations"u8))
            {
                if (addresses is not null)
                    throw InvalidReport();
                addresses = [];
                reader.ReadToken(JsonTokenType.StartArray);
                while (reader.ReadObjectItem())
                    ReadDeclaration(ref reader, annotatedIdentities, addresses);
            }
            else
                reader.SkipValue();
        }

        if (path is null || module is null || addresses is null)
            throw InvalidReport();
        if (!expected.TryGetValue(path, out var declarations))
            return;
        if (!seenPaths.Add(path)
            || !string.Equals(module, path[..^".lean".Length].Replace('/', '.'), StringComparison.Ordinal))
            throw new FormatException($"Truth release raw_lean_report has an invalid or duplicate module for '{path}'.");

        foreach (var (identity, expectedAddress) in declarations)
        {
            if (!addresses.TryGetValue(identity, out var address) || !string.Equals(address, expectedAddress, StringComparison.Ordinal))
                throw new FormatException("Truth release strict Omega official_statement_type_address disagrees with raw_lean_report "
                    + $"for '{path}' declaration '{identity.NameKey}' ({identity.Kind}).");
        }
    }

    private static void ReadDeclaration(
        ref ReportReader reader,
        HashSet<(string NameKey, string Kind)> annotatedIdentities,
        Dictionary<(string NameKey, string Kind), string?> addresses)
    {
        string? nameKey = null;
        string? kind = null;
        string? address = null;
        while (reader.ReadProperty())
        {
            if (reader.ValueIs("name_key"u8))
                reader.ReadStringOnce(ref nameKey);
            else if (reader.ValueIs("kind"u8))
                reader.ReadStringOnce(ref kind);
            else if (reader.ValueIs("type_sha256"u8))
                reader.ReadStringOnce(ref address);
            else
                reader.SkipValue();
        }

        if (nameKey is not null && kind is not null && annotatedIdentities.Contains((nameKey, kind))
            && !addresses.TryAdd((nameKey, kind), address))
            addresses[(nameKey, kind)] = null;
    }

    private static FormatException InvalidReport() => new("Truth release raw_lean_report has an invalid structure.");

    private ref struct ReportReader(Stream stream, IncrementalHash hash)
    {
        private byte[] buffer = new byte[16 * 1024];
        private Utf8JsonReader reader;
        private int buffered;
        private bool endOfStream;

        internal bool Read()
        {
            while (!reader.Read())
            {
                if (endOfStream)
                    return false;

                var state = reader.CurrentState;
                var consumed = (int)reader.BytesConsumed;
                buffered -= consumed;
                buffer.AsSpan(consumed, buffered).CopyTo(buffer);
                if (buffered == buffer.Length)
                    Array.Resize(ref buffer, checked(buffer.Length * 2));

                var count = stream.Read(buffer.AsSpan(buffered));
                hash.AppendData(buffer.AsSpan(buffered, count));
                buffered += count;
                endOfStream = count == 0;
                reader = new Utf8JsonReader(buffer.AsSpan(0, buffered), endOfStream, state);
            }
            return true;
        }

        internal void ReadToken(JsonTokenType type)
        {
            if (!Read() || reader.TokenType != type)
                throw InvalidReport();
        }

        internal bool ReadProperty() => ReadItem(JsonTokenType.PropertyName, JsonTokenType.EndObject);

        internal bool ReadObjectItem() => ReadItem(JsonTokenType.StartObject, JsonTokenType.EndArray);

        private bool ReadItem(JsonTokenType item, JsonTokenType end)
        {
            if (!Read() || (reader.TokenType != item && reader.TokenType != end))
                throw InvalidReport();
            return reader.TokenType == item;
        }

        internal bool ValueIs(ReadOnlySpan<byte> value) => reader.ValueTextEquals(value);

        internal void ReadStringOnce(ref string? value)
        {
            if (value is not null)
                throw InvalidReport();
            ReadToken(JsonTokenType.String);
            value = reader.GetString();
        }

        internal void SkipValue()
        {
            if (!Read())
                throw InvalidReport();
            if (reader.TokenType is JsonTokenType.StartArray or JsonTokenType.StartObject)
            {
                var depth = reader.CurrentDepth;
                while (Read())
                {
                    if (reader.CurrentDepth == depth)
                        return;
                }
                throw InvalidReport();
            }
        }
    }
}
