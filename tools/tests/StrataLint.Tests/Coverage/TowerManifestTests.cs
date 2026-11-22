using System.Text;
using StrataLint.Engine;

namespace StrataLint.Tests;

public sealed class TowerManifestTests
{
    [Fact]
    public void YamlDeclaresMembersAndEdgesWithoutEmbeddingValidationSemantics()
    {
        const string yaml = """
            schema_version: 1
            components:
              - id: sl-rules
                kind: rule-catalog
                members:
                  - SL-001
                  - SL-002
                judged_by:
                  - bootstrap-pr-1
                verification: verified
            bootstrap:
              id: bootstrap-pr-1
              judge: open
              reason: "Godel boundary: the trust root cannot prove its own consistency."
              commit: f3f471846dd81cfcc39ecaa386966fcf0b058464
              pull_request: 1
              verification: ASSUMED-UNVERIFIED
            """;

        var parsed = Assert.IsType<TowerManifestParseOutcome.Loaded>(
            TowerManifestParser.Parse(Encoding.UTF8.GetBytes(yaml)));

        var component = Assert.Single(parsed.Syntax.Components);
        Assert.Equal(new[] { "SL-001", "SL-002" }, component.Members);
        Assert.Equal(new[] { "bootstrap-pr-1" }, component.JudgedBy);
    }
}
