using StrataLint.Engine;

namespace StrataLint.Scribe.Tests;

public sealed class ContentCheckCommandTests
{
    [Theory]
    [InlineData("emit")]
    [InlineData("emit", "--check")]
    public void MissingManifestFailsBeforeRepositoryResolution(params string[] arguments)
    {
        using var root = new TemporaryRoot(sdkConfiguration: true);
        var error = new StringWriter();
        Assert.Equal(2, ScribeCli.Run(arguments, root.Path, TextWriter.Null, error));
        Assert.Contains("MissingPathsManifest", error.ToString(), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("content-check")]
    [InlineData("content-check", "--scoped", "--report", "report.json", "--targets-from", "targets")]
    [InlineData("content-check", "--scoped", "--report", "", "--targets-from", "targets", "--paths-from", "paths")]
    [InlineData("describe-report", "--scribe-pack", "pack.zip", "--scribe-pack-digest", "digest")]
    [InlineData("projections", "--check", "--report", "report.json")]
    [InlineData("markdown-check", "--report", "report.json", "--paths-from", "paths")]
    [InlineData("content-check", "--report")]
    [InlineData("content-check", "--report", "report.json", "--paths-from")]
    [InlineData("content-check", "--report", "report.json", "--bad", "paths.txt")]
    [InlineData("content-check", "--paths-from", "paths.txt", "--report", "report.json")]
    [InlineData("content-check", "--report", "report.json", "extra")]
    [InlineData("content-check", "--report", "", "--paths-from", "paths.txt")]
    [InlineData("content-check", "--report", "report.json", "--paths-from", " ")]
    public void RejectsBadArgumentsBeforeResolvingARepository(params string[] arguments)
    {
        using var root = new TemporaryRoot(sdkConfiguration: true);
        var error = new StringWriter();
        Assert.Equal(2, ScribeCli.Run(arguments, root.Path, TextWriter.Null, error));
        Assert.Contains("usage:", error.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain("Repository", error.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void ChecksOnlySelectedDefinitionAndDoesNotExecuteUnselectedDefinitions()
    {
        using var fixture = new Fixture();
        fixture.Write("Blueprint/D5/S0/Synthetic/Unselected.scribe.cs", "invalid C# // FormulaToken");
        fixture.Write("Blueprint/D5/S0/Synthetic/Unselected.md", "$$u_{n}_{i}$$");
        var output = new StringWriter();
        var error = new StringWriter();
        var exit = fixture.Run(Path, output, error);
        Assert.True(exit == 0, error.ToString());
        Assert.Contains("content-check: definitions=1", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown: judged=1", output.ToString(), StringComparison.Ordinal);
        Assert.Equal(1, fixture.Run("Blueprint/D5/S0/Synthetic/Unselected.scribe.cs", TextWriter.Null, new StringWriter()));
    }

    [Fact]
    public void EmptyManifestExecutesNoDefinitionsAndIgnoresUnselectedMarkdown()
    {
        using var fixture = new Fixture();
        fixture.Write(Path, "invalid C#");
        fixture.Write(Markdown, "$$u_{n}_{i}$$");
        var output = new StringWriter();
        var error = new StringWriter();
        Assert.Equal(0, fixture.Run("", output, error));
        Assert.Contains("content-check: definitions=0", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown: judged=0", output.ToString(), StringComparison.Ordinal);
        Assert.Empty(error.ToString());
    }

    [Fact]
    public void MarkdownOnlyPlainTextDoesNotExecuteDefinition()
    {
        using var fixture = new Fixture();
        fixture.Write(Path, UnselectedDefinition);
        fixture.Write(Markdown, "# Updated narrative\n\nOrdinary text.\n");
        var output = new StringWriter();
        var error = new StringWriter();

        Assert.True(fixture.Run(Markdown, output, error) == 0, error.ToString());
        Assert.Contains("content-check: definitions=0", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown: judged=1 formula(s)=0 red=0", output.ToString(), StringComparison.Ordinal);
        Assert.Empty(error.ToString());
    }

    [Fact]
    public void MarkdownOnlyInvalidFormulaIsRejectedWithoutExecutingDefinition()
    {
        using var fixture = new Fixture();
        fixture.Write(Path, UnselectedDefinition);
        fixture.Write(Markdown, "$$u_{n}_{i}$$");
        var output = new StringWriter();
        var error = new StringWriter();

        Assert.Equal(1, fixture.Run(Markdown, output, error));
        Assert.Contains("content-check: definitions=0", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown: judged=1 formula(s)=1 red=1", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown red " + Markdown, error.ToString(), StringComparison.Ordinal);
        Assert.Contains("Double subscript", error.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain("Compilation", error.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void ChecksSelectedDefinitionAndItsMarkdownFormula()
    {
        using var fixture = new Fixture();
        fixture.Write(Markdown, "$$u_{n}_{i}$$");
        var output = new StringWriter();
        var error = new StringWriter();
        Assert.Equal(1, fixture.Run(Path, output, error));
        Assert.Contains("content-check: definitions=1", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown: judged=1", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("Double subscript", error.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void ManyMarkdownOnlyChangesExecuteNoDefinitions()
    {
        using var fixture = new Fixture();
        var paths = new List<string>();
        for (var index = 0; index < 64; index++)
        {
            var stem = $"Blueprint/D5/S0/Synthetic/Reemitted{index}";
            fixture.Write(stem + ".scribe.cs", UnselectedDefinition);
            fixture.Write(stem + ".md", $"# Reemitted {index}\n\nA formula $x^2$.\n");
            paths.Add(stem + ".md");
        }
        var output = new StringWriter();
        var error = new StringWriter();

        Assert.True(fixture.Run(string.Join('\0', paths), output, error) == 0, error.ToString());
        Assert.Contains("content-check: definitions=0", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown: judged=64 formula(s)=64 red=0", output.ToString(), StringComparison.Ordinal);
        Assert.Empty(error.ToString());
    }

    [Fact]
    public void SelectedSourceGovernanceIsPreserved()
    {
        using var fixture = new Fixture();
        File.AppendAllText(fixture.Root.Resolve(Path), "\n// FormulaToken\n");
        var error = new StringWriter();
        Assert.Equal(1, fixture.Run(Path, TextWriter.Null, error));
        Assert.Contains("linear-formula-token", error.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void ExplicitMarkdownWithoutDefinitionIsRejected()
    {
        using var fixture = new Fixture();
        const string orphan = "Blueprint/D5/S0/Synthetic/Orphan.md";
        fixture.Write(orphan, "# Orphan");
        var output = new StringWriter();
        var error = new StringWriter();
        Assert.Equal(1, fixture.Run(orphan, output, error));
        Assert.Contains("content-check: definitions=0", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("markdown red " + orphan, error.ToString(), StringComparison.Ordinal);
        Assert.Contains("no Scribe document renders", error.ToString(), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("", 0, "markdown: judged=0")]
    [InlineData(Markdown, 1, "Double subscript")]
    [InlineData(Path, 2, "selected Scribe definitions require Lean targets")]
    public void ScopedCommandChecksEmptyTargetSelectionWithoutAReport(string selection, int expected, string message)
    {
        using var fixture = new Fixture();
        fixture.Write(Markdown, "$$u_{n}_{i}$$");
        var targets = fixture.Root.Resolve("targets");
        File.WriteAllText(targets, "");
        var output = new StringWriter();
        var error = new StringWriter();

        var exit = ScribeCli.Run(["content-check", "--scoped", "--targets-from", targets, "--paths-from", "-"],
            fixture.Root.Path, output, error, new StringReader(selection));

        Assert.Equal(expected, exit);
        Assert.Contains(message, output.ToString() + error, StringComparison.Ordinal);
    }

    [Fact]
    public void ScopedCommandRequiresAReportForNonemptyTargets()
    {
        using var fixture = new Fixture();
        var targets = fixture.Root.Resolve("targets");
        File.WriteAllText(targets, "D5/S0/Synthetic/CurrentMarkdown.lean\0");
        var error = new StringWriter();

        Assert.Equal(2, ScribeCli.Run(["content-check", "--scoped", "--targets-from", targets, "--paths-from", "-"],
            fixture.Root.Path, TextWriter.Null, error, new StringReader("")));
        Assert.Contains("nonempty Lean targets require --report", error.ToString(), StringComparison.Ordinal);
    }

    private const string Path = "Blueprint/D5/S0/Synthetic/CurrentMarkdown.scribe.cs";
    private const string Markdown = "Blueprint/D5/S0/Synthetic/CurrentMarkdown.md";
    private const string UnselectedDefinition = """
        using System;
        using StrataLint.Scribe;
        internal sealed class Unselected : IScribeDocumentDefinition
        {
            public DocumentDefinition Create() => throw new InvalidOperationException("definition must not execute");
        }
        """;

    private sealed class Fixture : IDisposable
    {
        internal TemporaryRoot Root { get; } = new(sdkConfiguration: true);
        internal readonly LeanAxiomReport Report = LeanAxiomReport.Create(new Dictionary<string, LeanFileReport>
            { ["D5/S0/Synthetic/CurrentMarkdown.lean"] = new([], []) });
        internal Fixture()
        {
            SyntheticScribeRepository.WriteInputs(Root.Path, MarkdownCurrentCommandTests.Definition());
            Write("global.json", "{}\n");
            Write("D5/S0/Synthetic/CurrentMarkdown.lean", "namespace D5.S0.Synthetic.CurrentMarkdown\n");
            Write(Path, $$"""
                using StrataLint.Scribe;
                using static StrataLint.Scribe.DefinitionDsl;
                {{"namespace StrataLint.Scribe.Blueprint.D5.S0.Synthetic;"}}
                internal sealed class CurrentMarkdown : IScribeDocumentDefinition
                {
                    public DocumentDefinition Create() => DocumentDefinition.Create(ScribeDocument.Create(
                        Header("D5/S0/Synthetic/CurrentMarkdown", "Current Markdown command fixture."),
                        H("Current Markdown"), Blocks(Paragraph(Text("A current formula "),
                        Math(FormulaDsl.Id("x")), Text(".")))));
                }
                """);
            foreach (var group in new[] { "pilot", "expansion" })
                Write($"Golden/Projection/statement-projection-{group}-v1.json",
                    $$"""{"schema":"statement-projection-{{group}}-fixture-v1","declarations":[]}""");
        }
        internal int Run(string selection, TextWriter output, TextWriter error) =>
            ScribeContentChecks.Run(Root.Path, MarkdownFormulaScope.ParsePaths(selection), Report,
                checkContent: true, output, error);
        internal void Write(string path, string text) => File.WriteAllText(Root.Resolve(path), text);
        public void Dispose() => Root.Dispose();
    }
}
