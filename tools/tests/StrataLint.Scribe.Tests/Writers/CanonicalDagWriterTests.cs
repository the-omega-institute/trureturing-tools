using System.Collections.Immutable;
using System.Text;
using System.Text.Json;
using StrataLint.Engine;
using Trureturing.Truth;

namespace StrataLint.Scribe.Tests;

public sealed class CanonicalDagWriterTests
{
    [Fact]
    public void MarkedDeclarationsRenderAsDistinctLiteralNamesInMarkdown()
    {
        Assert.True(RepoPath.TryCreate("D5/S0/Test/Names.lean", out var path));
        var dag = TruthDagProjection.Create(
            [TruthProjectionNode.Create(path!, null, TruthState.Closed, "D5.S0.Test.Names")], [], [],
            "sha256:" + new string('0', 64), ImmutableDictionary<RepoPath, int>.Empty.Add(path!, 0));

        const string controlKey = "ns(n0,10:x»\r\n\0\u202Ey)";
        var longName = new string('`', 1000) + "*literal*";
        var keys = new[]
        {
            "ns(ns(n0,3:Foo),3:a.b)",
            "ns(ns(ns(n0,3:Foo),1:a),1:b)",
            "ns(n0,16:*literal*<b>&``_)",
            "nn(ns(n0,3:Foo),1)",
            "ns(ns(n0,3:Foo),1:1)",
            "ns(n0,6:α₁?)",
            $"ns(n0,{longName.Length}:{longName})",
            controlKey,
        };
        var markers = keys.Select(key => new TruthGraphDeclaration(key, "theorem", dag.RootSha256)).ToImmutableArray();
        var graph = new TruthGraphSection(
            [new(path!.Value, null, "closed", "D5.S0.Test.Names", 0) { OmegaOnlyAxiomFreeDeclarations = markers }],
            [], [], new(1, 0, 0, 0));

        var markdown = Encoding.UTF8.GetString(CanonicalDagWriter.Write(dag, graph).AsSpan());
        const string prefix = "  - ";
        const string suffix = " (theorem) 「仅 Omega · 零公理」";
        var names = markdown.Split('\n').Where(line => line.EndsWith(suffix, StringComparison.Ordinal))
            .Select(line => line[prefix.Length..^suffix.Length]).ToArray();

        Assert.Equal(new[] { "`Foo.«a.b»`", "`Foo.a.b`" }, names[..2]);
        Assert.Equal(new[] { "`Foo.1`", "`Foo.«1»`", "`α₁?`" }, names[3..6]);
        Assert.Equal("<code>«*literal*<b>&``_»</code>", System.Net.WebUtility.HtmlDecode(names[2]));
        Assert.Equal("<code>«" + longName + "»</code>", System.Net.WebUtility.HtmlDecode(names[6]));
        Assert.DoesNotContain('*', names[6]);
        Assert.DoesNotContain('`', names[6]);
        Assert.Equal(controlKey, JsonSerializer.Deserialize<string>(names[^1][1..^1]));
        Assert.DoesNotContain("\r", markdown, StringComparison.Ordinal);
        Assert.DoesNotContain("\0", markdown, StringComparison.Ordinal);
        Assert.DoesNotContain("\u202E", markdown, StringComparison.Ordinal);
    }
}
