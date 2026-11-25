using System.Collections.Immutable;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Trureturing.Truth;

/// <summary>Measured strict-backend dependencies and the corresponding official statement address.</summary>
public sealed record StrictOmegaMeasurement(
    [property: JsonPropertyName("declaration_name_key")] string DeclarationNameKey,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("statement_type_address")] string StatementTypeAddress,
    [property: JsonPropertyName("official_statement_type_address")] string OfficialStatementTypeAddress,
    [property: JsonPropertyName("libraries")] ImmutableArray<string> Libraries,
    [property: JsonPropertyName("axioms")] ImmutableArray<string> Axioms)
{
    public void RequireValid(string declarationNameKey, string kind)
    {
        if (string.IsNullOrEmpty(DeclarationNameKey) || DeclarationNameKey != declarationNameKey || Kind != kind)
            throw new FormatException("strict_omega declaration identity must match its official declaration");
        TruthExportValidation.RequireKind(Kind);
        TruthExportValidation.RequireSha256Id(StatementTypeAddress, "strict_omega statement_type_address");
        TruthExportValidation.RequireSha256Id(OfficialStatementTypeAddress, "strict_omega official_statement_type_address");
        RequireSet(Libraries, "libraries");
        RequireSet(Axioms, "axioms");
    }

    public static StrictOmegaMeasurement Read(JsonElement element)
    {
        string[] keys = ["declaration_name_key", "kind", "statement_type_address", "official_statement_type_address", "libraries", "axioms"];
        if (element.ValueKind != JsonValueKind.Object || !element.EnumerateObject()
            .Select(static property => property.Name).Order(StringComparer.Ordinal)
            .SequenceEqual(keys.Order(StringComparer.Ordinal)))
            throw new FormatException("strict_omega must contain exactly its six measurement fields");
        var measurement = new StrictOmegaMeasurement(ReadString(element, "declaration_name_key"), ReadString(element, "kind"),
            ReadString(element, "statement_type_address"), ReadString(element, "official_statement_type_address"),
            ReadSet(element, "libraries"), ReadSet(element, "axioms"));
        measurement.RequireValid(measurement.DeclarationNameKey, measurement.Kind);
        return measurement;
    }

    private static string ReadString(JsonElement element, string key) =>
        element.GetProperty(key) is { ValueKind: JsonValueKind.String } value
            ? value.GetString()! : throw new FormatException("strict_omega " + key + " must be a string");

    private static ImmutableArray<string> ReadSet(JsonElement element, string key)
    {
        var value = element.GetProperty(key);
        if (value.ValueKind != JsonValueKind.Array)
            throw new FormatException("strict_omega " + key + " must be an array");
        return value.EnumerateArray().Select(item => item.ValueKind == JsonValueKind.String
            ? item.GetString()! : throw new FormatException("strict_omega " + key + " must contain strings")).ToImmutableArray();
    }

    private static void RequireSet(ImmutableArray<string> values, string field)
    {
        if (values.IsDefault)
            throw new FormatException("strict_omega " + field + " must be an array");
        string? previous = null;
        foreach (var value in values)
        {
            if (string.IsNullOrWhiteSpace(value) || value.Any(char.IsControl)
                || (previous is not null && StringComparer.Ordinal.Compare(previous, value) >= 0))
                throw new FormatException("strict_omega " + field + " must be an Ordinal sorted unique array of nonempty strings");
            previous = value;
        }
    }
}
