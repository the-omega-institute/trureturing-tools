using System.Collections.Immutable;
using System.Text;
using StrataLint.Engine;

namespace StrataLint.Scribe.Tests;

public sealed class DescribeReportTests
{
    private const string FormalPath = "D5/S1/Phase/Basic.lean";

    [Fact]
    public void ContentCheckRejectsLinearFormulaTokensAndBucketNamespace()
    {
        WithRepository(root =>
        {
            var blueprint = Path.Combine(root, "Blueprint", "D5", "S1", "Phase");
            TemporaryFileSystem.Directory.CreateDirectory(blueprint);
            TemporaryFileSystem.File.WriteAllText(
                Path.Combine(blueprint, "Basic.scribe.cs"),
                "namespace Wrong;\n// FormulaToken\n");

            var findings = DescribeContentGovernance.ValidateSources(root);

            Assert.Contains(findings, finding => finding.Code == "linear-formula-token");
            Assert.Contains(findings, finding => finding.Code == "blueprint-namespace");
        });
    }

    [Fact]
    public void IndependentInventoryRejectsMismatchedStatsAndSuspectedNovelNodes()
    {
        var document = ScribeDocument.Create(
            DefinitionDsl.Header("D5/S1/Phase/Basic", "Inventory fixture."),
            Heading.Create("Inventory"),
            BlockSequence.Create(
            [
                CreateDescribe(
                    "candidate",
                    "Candidate",
                    DescribeKind.Remark,
                    AssessedProvenance.NovelAfterSearch(
                        GidRef.Create("D5/S1/Phase/Basic"))),
            ]));
        var mismatched = new DescribeNodeStats(
            0,
            0,
            0,
            0,
            ImmutableSortedDictionary<string, int>.Empty,
            ImmutableSortedDictionary<string, int>.Empty);

        var findings = DescribeContentGovernance.ValidateIndependentInventory(
            [document],
            mismatched);

        Assert.Contains(findings, finding => finding.Code == "describe-ast-inventory");
        Assert.Contains(findings, finding => finding.Code == "suspected-novel");
    }

    [Fact]
    public void ReferencedLibraryNoteRequiresANonemptyLocatorSection()
    {
        WithRepository(root =>
        {
            var literature = LibraryNoteRef.Create("D5/L/sos1957threegap");
            var document = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "Locator fixture."),
                Heading.Create("Locator"),
                BlockSequence.Create(
                [
                    CreateDescribe(
                        "literature",
                        "Literature",
                        DescribeKind.Remark,
                        AssessedProvenance.FromLiterature(literature)),
                ]));
            var inspection = LibraryNoteCatalog.Inspect(root);

            var findings = DescribeContentGovernance.ValidateReferencedNoteLocators(
                root,
                [document],
                inspection);

            Assert.Contains(findings, finding => finding.Code == "incomplete-library-locator");
        });
    }

    [Theory]
    [InlineData("DOI 10.1007/BF01389053, Theorem 1.", true)]
    [InlineData("Source page https://example.org/source.", true)]
    [InlineData("DOI 10.1007/BF01389053. Source page https://example.org/Source.", true)]
    [InlineData("DOI 10.1007/BF01389053. Source page https://example.org/source.", false)]
    [InlineData("DOI 10.1007/bf01389053. Source page https://example.org/source.", false)]
    public void ReferencedLibraryNoteWithDoiAndUrlMustBindBothInItsLocator(string locator, bool incomplete)
    {
        WithRepository(root =>
        {
            TemporaryFileSystem.File.WriteAllText(
                Path.Combine(root, "Library", "notes", "sos1957threegap.md"),
                "---\n"
                + "bibkey: sos1957threegap\n"
                + "authors: Vera T. Sos\n"
                + "year: 1957\n"
                + "title: On the three gap theorem\n"
                + "doi: 10.1007/BF01389053\n"
                + "url: https://example.org/source\n"
                + "claim: Gap lengths for irrational rotations.\n"
                + "strata_touched:\n"
                + "  - D5/S1/Phase/Basic\n"
                + "license: citation-only\n"
                + "triage: anchor\n"
                + "---\n"
                + "\n## Verified locator\n\n" + locator + "\n",
                new UTF8Encoding(false, true));
            var literature = LibraryNoteRef.Create("D5/L/sos1957threegap");
            var document = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "Locator fixture."),
                Heading.Create("Locator"),
                BlockSequence.Create(
                [
                    CreateDescribe(
                        "literature",
                        "Literature",
                        DescribeKind.Remark,
                        AssessedProvenance.FromLiterature(literature)),
                ]));
            var inspection = LibraryNoteCatalog.Inspect(root);

            var findings = DescribeContentGovernance.ValidateReferencedNoteLocators(
                root,
                [document],
                inspection);

            Assert.Empty(inspection.Findings);
            Assert.Single(inspection.Notes);
            if (incomplete)
            {
                Assert.Contains(findings, finding => finding.Code == "incomplete-library-locator");
            }
            else
            {
                Assert.Empty(findings);
            }
        });
    }

    [Fact]
    public void ReportObservesTitleDerivedIdsAndCrossModuleDeclarationsWithoutBlocking()
    {
        WithRepository(root =>
        {
            var targetDirectory = Path.Combine(root, "D5", "S1", "Scale");
            TemporaryFileSystem.Directory.CreateDirectory(targetDirectory);
            TemporaryFileSystem.File.WriteAllText(
                Path.Combine(targetDirectory, "Embedding.lean"),
                "namespace D5.S1.Scale.Embedding\n");
            var document = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "Observation fixture."),
                Heading.Create("Observations"),
                DefinitionDsl.Blocks(Describe.Remark(
                    DescribeId.Create("same-title"),
                    DeclarationHandle.Create(
                        "D5/S1/Scale/Embedding.embedding_injective"),
                    Heading.Create("Same title"),
                    AssessedProvenance.FromRepo(),
                    DefinitionDsl.Blocks(DefinitionDsl.Paragraph(DefinitionDsl.Text("Content."))))));

            var report = DescribeReport.Build(root, [document]);

            Assert.Empty(report.RedFindings);
            Assert.Contains(report.Observations, static item =>
                item.Code == "title-derived-id" && item.Path.EndsWith("#describe/same-title", StringComparison.Ordinal));
            Assert.Contains(report.Observations, static item =>
                item.Code == "cross-module-lean-declaration" && item.Path.EndsWith("#describe/same-title", StringComparison.Ordinal));
        });
    }

    [Fact]
    public void ReportCountsSelectedNodesAndRecordsObservations()
    {
        WithRepository(root =>
        {
            var literature = LibraryNoteRef.Create("D5/L/sos1957threegap");
            var document = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "Report fixture."),
                Heading.Create("Report"),
                BlockSequence.Create(
                [
                    CreateDescribe("repository", "Repository", DescribeKind.Definition, AssessedProvenance.FromRepo()),
                    CreateDescribe("literature", "Literature", DescribeKind.Theorem, AssessedProvenance.FromLiterature(literature)),
                    CreateDescribe(
                        "candidate",
                        "Candidate",
                        DescribeKind.Lemma,
                        AssessedProvenance.NovelAfterSearch(GidRef.Create("D5/S1/Phase/Basic"))),
                    new DocumentBlock.Paragraph(InlineSequence.Create(
                    [
                        new Inline.Text(TextRun.Create("Code span `x = y` and Unicode φ.")),
                        new Inline.InlineFormula(new Formula.Phi()),
                    ])),
                    new DocumentBlock.DisplayFormula(new Formula.Phi()),
                ]));

            var report = DescribeReport.Build(root, [document]);
            Assert.Equal("classified", report.Status);
            Assert.Equal(3, report.NodeStats.Total);
            Assert.Equal(2, report.NodeStats.FormulaContentSlots);
            Assert.Equal(0, report.NodeStats.FormulaStatements);
            Assert.Equal(1, report.NodeStats.ByProvenance["repo-derived"]);
            Assert.Equal(1, report.NodeStats.ByProvenance["literature-attested"]);
            Assert.Equal(1, report.NodeStats.ByProvenance["suspected-novel"]);
            Assert.Single(report.SuspectedNovel);
            Assert.Empty(report.RedFindings);
            var observationCodes = report.Observations.Select(item => item.Code).ToArray();
            Assert.Contains("code-span", observationCodes);
            Assert.Contains("unicode-suspected-formula", observationCodes);
            Assert.Contains("lean-docstring-formula", observationCodes);
            Assert.Contains("online-doi-title-check", observationCodes);
        });
    }

    [Fact]
    public void ClassifiedReportHasNoOpenAssessedCountButKeepsNovelCandidatesQueryable()
    {
        WithRepository(root =>
        {
            var document = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "Classified fixture."),
                Heading.Create("Classified"),
                BlockSequence.Create(
                [
                    CreateDescribe(
                        "candidate",
                        "Candidate",
                        DescribeKind.Proposition,
                        AssessedProvenance.NovelAfterSearch(GidRef.Create("D5/S1/Phase/Basic"))),
                ]));

            var report = DescribeReport.Build(root, [document]);

            Assert.Equal("classified", report.Status);
            Assert.Single(report.SuspectedNovel);
        });
    }

    [Fact]
    public void ReportUsesLeanMaterialToRejectAMissingDeclarationSelector()
    {
        WithRepository(root =>
        {
            var document = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "Selector fixture."),
                Heading.Create("Selector"),
                BlockSequence.Create(
                [
                    Describe.Lean(
                        DescribeId.Create("missing-declaration"),
                        DeclarationHandle.Create(
                            "D5/S1/Phase/Basic.missing_declaration"),
                        Heading.Create("Missing declaration"),
                        StatementSource.FromAuthor(InlineIdentity()),
                        AssessedProvenance.FromRepo(),
                        BlockSequence.Create(
                        [
                            DefinitionDsl.Paragraph(DefinitionDsl.Text("Missing selector fixture.")),
                        ]),
                        DescribeRole.Theorem
                    ),
                ]));
            var leanReport = LeanAxiomReport.Create(
                new Dictionary<string, LeanFileReport>(StringComparer.Ordinal)
                {
                    [FormalPath] = new LeanFileReport([], []),
                });

            var report = DescribeReport.Build(root, [document], leanReport);

            Assert.Contains(report.RedFindings, finding => finding.Code == "dangling-gid");
        });
    }

    [Theory]
    [InlineData("not-a-doi", "invalid-doi")]
    [InlineData("10.1007/BF01389053", "duplicate-doi")]
    public void ReportRecordsDoiViolationsAsRedFindings(
        string firstDoi,
        string expectedCode)
    {
        WithRepository(
            root =>
            {
                if (expectedCode == "duplicate-doi")
                {
                    WriteNote(
                        root,
                        "slater1967gaps",
                        "Second title",
                        "10.1007/bf01389053");
                }

                var report = DescribeReport.Build(root, []);

                Assert.Equal("invalid", report.Status);
                Assert.Contains(report.RedFindings, finding => finding.Code == expectedCode);
            },
            doi: firstDoi);
    }

    [Fact]
    public void InsertingAPrecedingDescribeDoesNotRenumberExistingNodeIds()
    {
        WithRepository(root =>
        {
            var existing = CreateDescribe(
                "existing-claim",
                "Existing",
                DescribeKind.Remark,
                AssessedProvenance.FromRepo());
            var before = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "Before fixture."),
                Heading.Create("Before"),
                BlockSequence.Create([existing]));
            var after = ScribeDocument.Create(
                DefinitionDsl.Header("D5/S1/Phase/Basic", "After fixture."),
                Heading.Create("After"),
                BlockSequence.Create(
                [
                    CreateDescribe(
                        "new-claim",
                        "New",
                        DescribeKind.Remark,
                        AssessedProvenance.FromRepo()),
                    existing,
                ]));

            var beforeId = Assert.Single(DescribeReport.Build(root, [before]).Nodes).NodeId;
            var afterId = Assert.Single(
                DescribeReport.Build(root, [after]).Nodes,
                node => node.Title == "Existing").NodeId;

            Assert.Equal("D5/S1/Phase/Basic#describe/existing-claim", beforeId);
            Assert.Equal(beforeId, afterId);
        });
    }

    private static DocumentBlock.Describe CreateDescribe(
        string id,
        string title,
        DescribeKind kind,
        AssessedProvenance provenance)
    {
        var describeId = DescribeId.Create(id);
        var heading = Heading.Create(title);
        var content = DefinitionDsl.Blocks(
            DefinitionDsl.Paragraph(DefinitionDsl.Text("Typed narrative.")));
        var handle = DeclarationHandle.Create("D5/S1/Phase/Basic.fixture_claim");
        var latex = InlineIdentity();
        return kind switch
        {
            DescribeKind.Definition => Describe.Lean(
                describeId, handle, heading, StatementSource.WithoutFormula(),
                provenance, content, DescribeRole.Definition),
            DescribeKind.Theorem => Describe.Lean(
                describeId, handle, heading, StatementSource.FromAuthor(latex),
                provenance, content, DescribeRole.Theorem),
            DescribeKind.Proposition => Describe.Lean(
                describeId, handle, heading, StatementSource.FromAuthor(latex),
                provenance, content, DescribeRole.Proposition),
            DescribeKind.Lemma => Describe.Lean(
                describeId, handle, heading, StatementSource.FromAuthor(latex),
                provenance, content, DescribeRole.Lemma),
            DescribeKind.Example => Describe.Example(
                describeId, heading, new Formula.Phi(), provenance, content),
            DescribeKind.Remark => Describe.Remark(
                describeId, heading, new Formula.Phi(), provenance, content),
            _ => throw new ArgumentOutOfRangeException(nameof(kind)),
        };
    }

    private static Formula InlineIdentity() => new Formula.Layout(
        FormulaLayoutMode.Inline,
        new Formula.Relation(
            new Formula.Symbol(FormulaIdentifier.Create("x")),
            FormulaRelationOperator.Equal,
            new Formula.Symbol(FormulaIdentifier.Create("x"))));

    private static void WithRepository(
        Action<string> assertion,
        string doi = "10.1007/BF01389053")
    {
        var root = Path.Combine(Path.GetTempPath(), "stratalint-report-" + Guid.NewGuid().ToString("N"));
        var formalPath = Path.Combine(root, "D5", "S1", "Phase", "Basic.lean");
        var notes = Path.Combine(root, "Library", "notes");
        TemporaryFileSystem.Directory.CreateDirectory(Path.GetDirectoryName(formalPath)!);
        TemporaryFileSystem.Directory.CreateDirectory(notes);
        TemporaryFileSystem.Directory.CreateDirectory(Path.Combine(root, "Blueprint"));
        TemporaryFileSystem.File.WriteAllText(Path.Combine(root, "global.json"), "{}\n", new UTF8Encoding(false, true));
        TemporaryFileSystem.File.WriteAllText(
            formalPath,
            "/-- Formula x = y in a Lean docstring. -/\nnamespace D5.S1.Phase\n",
            new UTF8Encoding(false, true));
        WriteNote(
            root,
            "sos1957threegap",
            "On the three gap theorem",
            doi);
        try
        {
            assertion(root);
        }
        finally
        {
            TemporaryFileSystem.Directory.Delete(root, recursive: true);
        }
    }

    private static void WriteNote(
        string root,
        string bibkey,
        string title,
        string doi)
    {
        var notes = Path.Combine(root, "Library", "notes");
        TemporaryFileSystem.Directory.CreateDirectory(notes);
        TemporaryFileSystem.File.WriteAllText(
            Path.Combine(notes, bibkey + ".md"),
            "---\n"
            + $"bibkey: {bibkey}\n"
            + "authors: Vera T. Sos\n"
            + "year: 1957\n"
            + $"title: {title}\n"
            + $"doi: {doi}\n"
            + "claim: Gap lengths for irrational rotations.\n"
            + "strata_touched:\n"
            + "  - D5/S1/Phase/Basic\n"
            + "license: citation-only\n"
            + "triage: anchor\n"
            + "---\n",
            new UTF8Encoding(false, true));
    }
}
