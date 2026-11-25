using System.Collections.Immutable;
using System.Text;
using StrataLint.Engine;
using Trureturing.Truth;

namespace StrataLint.Tests;

public sealed class FrozenStateContractTests
{
    private const string Path = "Golden/Frozen/state/D5/S0/Carrier/Probe.lean.json";
    private const string Pin = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    [Fact]
    public void StrictStateSchemaRejectsSingleFieldRecord()
    {
        Assert.Throws<FormatException>(() => FrozenStateRecordLoader.Load(File($"{{\"statement_id\":\"{Pin}\"}}\n")));
    }

    [Fact]
    public void StrictStateSchemaAcceptsOnlyStatementAndSortedDeclarations()
    {
        var text = $"{{\"statement_id\":\"{Pin}\",\"declarations\":[{{\"declaration_name_key\":\"ns(n0,5:probe)\",\"kind\":\"theorem\"}}]}}\n";
        Assert.Equal(Pin, FrozenStateRecordLoader.Load(File(text)).StatementId.Value);
    }

    [Theory]
    [InlineData("axiom")]
    [InlineData("constructor")]
    [InlineData("def")]
    [InlineData("inductive")]
    [InlineData("opaque")]
    [InlineData("quotient")]
    [InlineData("recursor")]
    [InlineData("theorem")]
    public void StrictStateSchemaAcceptsEveryProducerDeclarationKind(string kind)
    {
        var text = $"{{\"statement_id\":\"{Pin}\",\"declarations\":[{{\"declaration_name_key\":\"ns(n0,5:probe)\",\"kind\":\"{kind}\"}}]}}\n";

        var record = FrozenStateRecordLoader.Load(File(text));

        Assert.Equal(new FrozenDeclaration("ns(n0,5:probe)", kind), Assert.Single(record.Declarations));
        Assert.Equal(Encoding.UTF8.GetBytes(text), FrozenStateRecord.Encode(record.StatementId, record.Declarations).ToArray());
    }

    [Theory]
    [InlineData("quot")]
    [InlineData("definition")]
    [InlineData("unknown")]
    public void StrictStateSchemaRejectsUnsupportedDeclarationKind(string kind)
    {
        var text = $"{{\"statement_id\":\"{Pin}\",\"declarations\":[{{\"declaration_name_key\":\"ns(n0,5:probe)\",\"kind\":\"{kind}\"}}]}}\n";

        Assert.Throws<FormatException>(() => FrozenStateRecordLoader.Load(File(text)));
    }

    [Theory]
    [InlineData("[{\"declaration_name_key\":\"ns(n0,1:b)\",\"kind\":\"theorem\"},{\"declaration_name_key\":\"ns(n0,1:a)\",\"kind\":\"theorem\"}]")]
    [InlineData("[{\"declaration_name_key\":\"ns(n0,1:a)\",\"kind\":\"theorem\"},{\"declaration_name_key\":\"ns(n0,1:a)\",\"kind\":\"theorem\"}]")]
    [InlineData("[{\"declaration_name_key\":\"ns(n0,1:a)\",\"kind\":\"theorem\",\"statement_id\":\"unused\"}]")]
    [InlineData("[{\"declaration_name_key\":\"broken\",\"kind\":\"theorem\"}]")]
    public void StrictStateSchemaRejectsUnorderedDuplicateOrExtraDeclarationFields(string declarations)
    {
        Assert.Throws<FormatException>(() => FrozenStateRecordLoader.Load(File(
            $"{{\"statement_id\":\"{Pin}\",\"declarations\":{declarations}}}\n")));
    }

    [Fact]
    public void StateWriterProducesTheClosedSchema()
    {
        using var temporary = new TemporaryDirectory();
        Assert.True(FrozenStateWriter.Write(temporary.Path, RepoPath.CreateKnown("D5/S0/Carrier/Probe.lean"), StatementId.Create(Pin)));
        var wire = System.IO.File.ReadAllText(System.IO.Path.Combine(temporary.Path, Path));
        Assert.Equal($"{{\"statement_id\":\"{Pin}\",\"declarations\":[]}}\n", wire);
    }

    [Fact]
    public void LegacyAndMeasuredDeclarationsRoundTripWithDistinctEmptyAndOmittedSets()
    {
        var text = $"{{\"statement_id\":\"{Pin}\",\"declarations\":["
            + "{\"declaration_name_key\":\"ns(n0,1:a)\",\"kind\":\"theorem\"},"
            + "{\"declaration_name_key\":\"ns(n0,1:b)\",\"kind\":\"theorem\",\"libraries\":[],\"axioms\":[]},"
            + "{\"declaration_name_key\":\"ns(n0,1:c)\",\"kind\":\"theorem\",\"libraries\":[\"Init\",\"Mathlib\"],\"axioms\":[\"propext\"]}]}\n";
        var record = FrozenStateRecordLoader.Load(File(text));
        Assert.Null(record.Declarations[0].Libraries);
        Assert.Null(record.Declarations[0].Axioms);
        Assert.Empty(record.Declarations[1].Libraries!.Value);
        Assert.Empty(record.Declarations[1].Axioms!.Value);
        Assert.Equal(Encoding.UTF8.GetBytes(text), FrozenStateRecord.Encode(record.StatementId, record.Declarations).ToArray());
    }

    [Fact]
    public void PairedRecordRoundTripsFullOfficialAndEmptyStrictAxiomArrays()
    {
        const string key = "ns(n0,1:a)";
        using var temporary = new TemporaryDirectory();
        var strict = new StrictOmegaMeasurement(key, "theorem", Pin, "sha256:" + new string('b', 64),
            ["OmegaInit", "OmegaMath"], []);
        var declarations = ImmutableArray.Create(new FrozenDeclaration(key, "theorem", ["Init", "OmegaMath"],
            ["Classical.choice", "Quot.sound", "propext"], strict), new FrozenDeclaration("ns(n0,1:b)", "theorem"));
        var module = RepoPath.CreateKnown("D5/S0/Carrier/Probe.lean");
        Assert.True(FrozenStateWriter.Write(temporary.Path, module, StatementId.Create(Pin), declarations));
        var text = System.IO.File.ReadAllText(System.IO.Path.Combine(temporary.Path, Path));
        var read = FrozenStateRecordLoader.Load(File(text));
        var measured = read.Declarations[0];
        Assert.Equal(declarations[0].Axioms!.Value.ToArray(), measured.Axioms!.Value.ToArray());
        Assert.Equal(strict.StatementTypeAddress, measured.StrictOmega!.StatementTypeAddress);
        Assert.Equal(strict.OfficialStatementTypeAddress, measured.StrictOmega.OfficialStatementTypeAddress);
        Assert.Equal(strict.Libraries.ToArray(), measured.StrictOmega.Libraries.ToArray());
        Assert.Empty(measured.StrictOmega.Axioms);
        Assert.Null(read.Declarations[1].StrictOmega);
        Assert.False(FrozenStateWriter.Write(temporary.Path, module, read.StatementId, read.Declarations));
    }

    [Theory]
    [InlineData("name")]
    [InlineData("kind")]
    [InlineData("address")]
    [InlineData("official-address")]
    [InlineData("missing-axioms")]
    [InlineData("extra-field")]
    [InlineData("null-libraries")]
    [InlineData("duplicate-axioms")]
    [InlineData("missing-primary")]
    public void RejectsInvalidPairedRecords(string defect)
    {
        const string key = "ns(n0,1:a)";
        var strict = new StrictOmegaMeasurement(key, "theorem", Pin, Pin, ["OmegaMath"], []);
        var canonical = FrozenStateRecord.Encode(StatementId.Create(Pin), [new(key, "theorem", [], [], strict)]);
        var root = System.Text.Json.Nodes.JsonNode.Parse(Encoding.UTF8.GetString(canonical.AsSpan()))!;
        var declaration = root["declarations"]![0]!.AsObject();
        var nested = declaration["strict_omega"]!.AsObject();
        switch (defect)
        {
            case "name": nested["declaration_name_key"] = "ns(n0,1:b)"; break;
            case "kind": nested["kind"] = "def"; break;
            case "address": nested["statement_type_address"] = "sha256:bad"; break;
            case "official-address": nested["official_statement_type_address"] = "sha256:bad"; break;
            case "missing-axioms": nested.Remove("axioms"); break;
            case "extra-field": nested["replayed"] = true; break;
            case "null-libraries": nested["libraries"] = null; break;
            case "duplicate-axioms": nested["axioms"] = new System.Text.Json.Nodes.JsonArray("propext", "propext"); break;
            case "missing-primary": declaration.Remove("axioms"); break;
        }
        Assert.Throws<FormatException>(() => FrozenStateRecordLoader.Load(File(root.ToJsonString() + "\n")));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void CurrentStateChecksPairedOfficialStatementAddress(bool stale)
    {
        var fixture = new RuleFixture();
        const string module = "D5/S0/Carrier/Probe.lean";
        fixture.Files[module] = "theorem probe : True := by trivial\n";
        var declaration = new LeanDeclaration("probe", "theorem", "True", ["propext"]) { NameKey = "ns(n0,5:probe)" };
        var report = new LeanFileReport([], [declaration]);
        fixture.Reports[module] = report;
        var pin = FrozenContentAddress.ComputeModuleStatementId(RepoPath.CreateKnown(module), report);
        var strict = new StrictOmegaMeasurement(declaration.NameKey, declaration.Kind, Pin,
            stale ? Pin : declaration.StatementTypeAddress, ["OmegaMath"], []);
        fixture.Files[Path] = Encoding.UTF8.GetString(FrozenStateRecord.Encode(pin,
            [new(declaration.NameKey, declaration.Kind, ["Init", "OmegaMath"], ["propext"], strict)]).AsSpan());
        var result = RuleCatalog.Default.EvaluateSingle(RuleId.CreateKnown(8), fixture.Build(RawChangeSet.Create([Path])));
        if (stale)
            Assert.Contains(result.Diagnostics, diagnostic => diagnostic.Message.Contains("strict_omega official statement address", StringComparison.Ordinal));
        else
            Assert.Empty(result.Diagnostics);
    }

    [Theory]
    [InlineData("\"libraries\":null")]
    [InlineData("\"libraries\":[\"OmegaInit\",\"OmegaInit\"]")]
    [InlineData("\"libraries\":[\"OmegaMath\",\"OmegaInit\"]")]
    [InlineData("\"libraries\":[0]")]
    [InlineData("\"axioms\":[\"Classical.choice\",\"Quot.sound\",\"propext\"]")]
    [InlineData("\"axioms\":[\"sorryAx\"]")]
    [InlineData("\"axioms\":[],\"libraries\":[]")]
    public void RejectsNoncanonicalAnnotations(string optional)
    {
        var text = $"{{\"statement_id\":\"{Pin}\",\"declarations\":[{{\"declaration_name_key\":\"ns(n0,1:a)\",\"kind\":\"theorem\",{optional}}}]}}\n";
        Assert.Throws<FormatException>(() => FrozenStateRecordLoader.Load(File(text)));
    }

    [Fact]
    public void CurrentStateAllowsAgedMeasurements()
    {
        var fixture = new RuleFixture();
        const string module = "D5/S0/Carrier/Probe.lean";
        fixture.Files[module] = "theorem probe : True := by trivial\n";
        var report = new LeanFileReport([], [new LeanDeclaration("probe", "theorem", "True", ["propext"])
            { NameKey = "ns(n0,5:probe)" }]);
        fixture.Reports[module] = report;
        var pin = FrozenContentAddress.ComputeModuleStatementId(RepoPath.CreateKnown(module), report);
        fixture.Files[Path] = Encoding.UTF8.GetString(FrozenStateRecord.Encode(pin,
            [new("ns(n0,5:probe)", "theorem", ["Mathlib"], [])]).AsSpan());
        Assert.Empty(RuleCatalog.Default.EvaluateSingle(RuleId.CreateKnown(8),
            fixture.Build(RawChangeSet.Create([Path]))).Diagnostics);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void CurrentStateChecksPinAndDeclarationInventoryIndependently(bool wrongPin)
    {
        var fixture = new RuleFixture();
        const string module = "D5/S0/Carrier/Probe.lean";
        fixture.Files[module] = "theorem probe : True := by trivial\n";
        fixture.Reports[module] = new LeanFileReport([], [new LeanDeclaration("probe", "theorem", "True", [])
        { NameKey = "ns(n0,5:probe)" }]);
        var pin = FrozenContentAddress.ComputeModuleStatementId(RepoPath.CreateKnown(module), fixture.Reports[module]).Value;
        fixture.Files[Path] = wrongPin
            ? "{\"statement_id\":\"" + Pin + "\",\"declarations\":[{\"declaration_name_key\":\"ns(n0,5:probe)\",\"kind\":\"theorem\"}]}\n"
            : "{\"statement_id\":\"" + pin + "\",\"declarations\":[]}\n";
        var findings = RuleCatalog.Default.EvaluateSingle(RuleId.CreateKnown(8), fixture.Build(RawChangeSet.Create([Path]))).Diagnostics;
        Assert.Contains(findings, diagnostic => diagnostic.Message.Contains(
            wrongPin ? "pin mismatch" : "declaration list mismatch", StringComparison.Ordinal));
    }

    [Fact]
    public void RepositoryStateRecordsRoundTripThroughTheStrictLoader()
    {
        var root = TestRepositoryLayout.FindRoot();
        var paths = Directory.EnumerateFiles(System.IO.Path.Combine(root, FrozenStatePath.Root), "*", SearchOption.AllDirectories).ToArray();
        Assert.NotEmpty(paths);
        foreach (var path in paths)
        {
            var relative = System.IO.Path.GetRelativePath(root, path).Replace('\\', '/');
            var bytes = System.IO.File.ReadAllBytes(path);
            var record = FrozenStateRecordLoader.Load(new(RepoPath.CreateKnown(relative), ImmutableArray.CreateRange(bytes), Encoding.UTF8.GetString(bytes)));
            Assert.Equal(bytes, FrozenStateRecord.Encode(record.StatementId, record.Declarations).ToArray());
        }
    }

    private static RepositoryFile File(string text) => new(RepoPath.CreateKnown(Path),
        ImmutableArray.CreateRange(Encoding.UTF8.GetBytes(text)), text);
}
