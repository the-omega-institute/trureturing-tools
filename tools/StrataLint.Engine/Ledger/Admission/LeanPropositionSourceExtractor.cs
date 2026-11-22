using System.Collections.Immutable;
using System.Text.RegularExpressions;

namespace StrataLint.Engine;

internal static class LeanSourceCatalog
{
    internal static ImmutableArray<string> ParseFileImports(RepositoryFile file, bool includeImplicitInit = false)
    {
        ArgumentNullException.ThrowIfNull(file);

        // Imports belong to the module header, not to later commands or quotations.
        // Read lazily so arbitrary body syntax need not be tokenized to read base data.
        // Lean.Parser.Module.Syntax: [module] [prelude] ([public] [meta] import [all] ident)*.
        using var tokens = LeanSourceTokenizer.ReadTokens(file.Text, importHeader: true).GetEnumerator();
        var current = tokens.MoveNext() ? tokens.Current : null;
        Consume("module");
        var prelude = Consume("prelude");
        var imports = ImmutableArray.CreateBuilder<string>();
        if (includeImplicitInit && !prelude) imports.Add("Init");
        while (current is not null)
        {
            Consume("public");
            Consume("meta");
            if (!Consume("import")) break;
            Consume("all");
            if (current is null || !IsIdentifierStart(current.Text[0]))
                throw new LeanSourceExtractionException("Lean import requires a module identifier.");
            // Escaping a plain name segment does not change the imported module.
            // Preserve escapes containing punctuation/whitespace, which name different segments.
            imports.Add(Regex.Replace(current.Text, @"«([\p{L}\p{Nl}_][\p{L}\p{Nl}\p{Nd}\p{Mn}\p{Mc}\p{No}_']*)»", "$1",
                RegexOptions.CultureInvariant));
            current = tokens.MoveNext() ? tokens.Current : null;
        }

        return imports.Distinct(StringComparer.Ordinal).ToImmutableArray();

        bool Consume(string keyword)
        {
            if (current?.Text != keyword) return false;
            current = tokens.MoveNext() ? tokens.Current : null;
            return true;
        }
    }

    private static bool IsIdentifierStart(char value) =>
        value == '_' || char.IsLetter(value) || value > 127 && !char.IsWhiteSpace(value);
}

internal sealed class LeanSourceExtractionException(string message) : FormatException(message);
