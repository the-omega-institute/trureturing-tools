using System.Collections.Immutable;
using System.IO.Compression;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using StrataLint.Cli;
using StrataLint.Engine;
using Trureturing.Truth;
using static StrataLint.TestSupport.FrozenLedgerTestData;

namespace StrataLint.Tests;

public sealed class LedgerAlignWriterTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void MissingExplicitModuleSelectionIsRejectedWithoutReadsOrWrites(bool hasFrozenMembers)
    {
        var original = Module("A");
        using var fixture = new AlignFixture(original with { StatementMaterial = "A changed" });
        if (hasFrozenMembers) fixture.Install(BuildCatalog(original));
        var before = fixture.Image();

        var result = fixture.Align();

        Assert.False(result.Success);
        Assert.Contains("USAGE", result.Error, StringComparison.Ordinal);
        Assert.Equal(0, fixture.Repository.ReadCount);
        Assert.Equal(before, fixture.Image());
    }

    [Fact]
    public void RepinWritesOnlyTargetWithFrozenDownstreamAndIdenticalRetryWritesNothing()
    {
        var original = Module("A");
        var current = original with { StatementMaterial = "True = True" };
        var downstream = Module("B", source: "import D5.S0.Carrier.A\ntheorem b : True := by trivial\n", imports: ["A"]);
        using var fixture = new AlignFixture(current, downstream);
        fixture.Install(BuildCatalog(original, downstream));
        var before = fixture.Image();
        var result = fixture.Align("--selector", PathFor("A"));
        Assert.True(result.Success, result.Error);
        var after = fixture.Image();
        Assert.Equal([FrozenStatePath.FromModulePath(RepoPathFor("A")).Value],
            after.Keys.Where(path => before[path] != after[path]));
        var timestamp = File.GetLastWriteTimeUtc(fixture.StateFile("A"));
        var retry = fixture.Align("--selector", PathFor("A"));
        Assert.True(retry.Success, retry.Error);
        Assert.Contains("changed=0", retry.Output, StringComparison.Ordinal);
        Assert.Equal(timestamp, File.GetLastWriteTimeUtc(fixture.StateFile("A")));
        Assert.Equal(after, fixture.Image());
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void OrdinaryAlignmentRetainsAnnotationsOnlyForTheSamePin(bool changesPin)
    {
        var baseline = Module("A");
        var current = changesPin ? baseline with { StatementMaterial = "True = True" } : baseline;
        using var fixture = new AlignFixture(current);
        fixture.Install(BuildCatalog(baseline));
        var path = fixture.StateFile("A");
        var record = JsonNode.Parse(File.ReadAllText(path))!;
        var declaration = record["declarations"]![0]!;
        declaration["libraries"] = new JsonArray("Init");
        declaration["axioms"] = new JsonArray("propext");
        var annotated = record.ToJsonString() + "\n";
        File.WriteAllText(path, annotated);

        var result = fixture.Align("--selector", PathFor("A"));

        Assert.True(result.Success, result.Error);
        if (changesPin)
        {
            var expected = Assert.Single(StateFiles(BuildCatalog(current)));
            Assert.Equal(expected.RawBytes.AsSpan().ToArray(), File.ReadAllBytes(path));
        }
        else
        {
            Assert.Equal(annotated, File.ReadAllText(path));
            Assert.Contains("changed=0", result.Output, StringComparison.Ordinal);
        }
    }

    [Fact]
    public void DifferentModulesHaveDisjointWriteSets()
    {
        var oldA = Module("A");
        var oldB = Module("B");
        using var first = new AlignFixture(oldA with { StatementMaterial = "A changed" }, oldB);
        using var second = new AlignFixture(oldA, oldB with { StatementMaterial = "B changed" });
        var baseline = BuildCatalog(oldA, oldB);
        first.Install(baseline); second.Install(baseline);
        var beforeFirst = first.Image(); var beforeSecond = second.Image();
        Assert.True(first.Align("--selector", PathFor("A")).Success);
        Assert.True(second.Align("--selector", PathFor("B")).Success);
        var writesA = first.Image().Where(pair => beforeFirst[pair.Key] != pair.Value).Select(static pair => pair.Key).ToArray();
        var writesB = second.Image().Where(pair => beforeSecond[pair.Key] != pair.Value).Select(static pair => pair.Key).ToArray();
        Assert.Single(writesA); Assert.Single(writesB); Assert.Empty(writesA.Intersect(writesB));
    }

    [Fact]
    public void ScopedAddAllowsClosedUnregisteredImportsAndIgnoresUnrelatedOpenModule()
    {
        var a = Module("A", source: "import D5.S0.Carrier.B\ntheorem a : True := by trivial\n", imports: ["B"]);
        using var fixture = new AlignFixture(a, Module("B"), Module("C", axioms: ["sorryAx"]));
        var result = fixture.Align("--add", PathFor("A"));
        Assert.True(result.Success, result.Error);
        Assert.Single(fixture.Image());
        Assert.True(File.Exists(fixture.StateFile("A")));
    }

    [Fact]
    public void ScopedTruthRetainsInventoryWhileReadingOnlyRequestedImportBodies()
    {
        var a = Module("A", source: "import D5.S0.Carrier.B\ntheorem a : True := by trivial\n", imports: ["B"]);
        using var fixture = new AlignFixture(a, Module("B"), Module("C"));
        var report = new DagLedgerCommandPreparation.FileLeanReportSource(
            fixture.ReportPath(scoped: true, "--add", PathFor("A")));

        var truth = DagLedgerCommandPreparation.BuildTruthScoped(
            fixture.Repository, report, [RepoPathFor("A")]);

        Assert.True(truth.Snapshot.Files[RepoPathFor("A")].ContentWasRead);
        Assert.True(truth.Snapshot.Files[RepoPathFor("B")].ContentWasRead);
        Assert.False(truth.Snapshot.Files[RepoPathFor("C")].ContentWasRead);
        Assert.True(truth.Snapshot.Files[RepoPath.CreateKnown("lean-toolchain")].ContentWasRead);
        Assert.True(truth.Snapshot.Files[RepoPath.CreateKnown("lake-manifest.json")].ContentWasRead);
        Assert.Equal(new[] { PathFor("A"), PathFor("B") },
            fixture.Repository.ScopedCurrentReads.SelectMany(static paths => paths)
                .Select(static path => path.Replace(":(literal)", "", StringComparison.Ordinal))
                .Order(StringComparer.Ordinal));
        Assert.Equal(new[] { RepoPathFor("A"), RepoPathFor("B") },
            truth.Report.Files.Keys.OrderBy(static path => path.Value, StringComparer.Ordinal));
    }

    [Fact]
    public void FullBundleIsRejectedWithoutReadingUnselectedSources()
    {
        using var fixture = new AlignFixture(Module("A"), Module("C"));
        var path = fixture.ReportPath(scoped: false, "--add", PathFor("A"));

        AssertRejectedBeforeAnyWrite(fixture, path, RawLeanReportArtifact.ScopedSchema);
        Assert.Equal(1, fixture.Repository.WholeTreeReadCount);
    }

    [Fact]
    public void ScopedBundleAddsOnlySelectedOwner()
    {
        var a = Module("A", source: "import D5.S0.Carrier.B\ntheorem a : True := by trivial\n", imports: ["B"]);
        var b = Module("B");
        var c = Module("C") with { StatementMaterial = "True = True" };
        using var fixture = new AlignFixture(a, b, c);
        var path = fixture.ReportPath(scoped: true, "--add", PathFor("A"));

        var result = fixture.Invoke(["--add", PathFor("A"), "--candidate-lean-report", path]);

        Assert.True(result.Success, result.Error);
        Assert.Contains("added=1", result.Output, StringComparison.Ordinal);
        var expected = StateFiles(BuildCatalog(a, b, c)).Single(file => file.Path ==
            FrozenStatePath.FromModulePath(RepoPathFor("A")));
        var original = FrozenStateRecordLoader.Load(expected);
        var bytes = File.ReadAllBytes(fixture.StateFile("A")).ToImmutableArray();
        var measured = FrozenStateRecordLoader.Load(new(expected.Path, bytes, Encoding.UTF8.GetString(bytes.AsSpan())));
        Assert.Equal(original.StatementId, measured.StatementId);
        Assert.True(original.HasInventory(measured.Declarations));
        Assert.All(measured.Declarations, declaration =>
        {
            Assert.Empty(declaration.Libraries!.Value);
            Assert.Empty(declaration.Axioms!.Value);
        });
        Assert.Equal(expected.Path.Value, Assert.Single(fixture.Image()).Key);
    }

    [Fact]
    public void CompiledResponseWritesPairedAndOfficialOnlyRecordsWithExactMeasuredArrays()
    {
        var full = new[] { "Classical.choice", "Quot.sound", "propext" }.ToImmutableArray();
        using var fixture = new AlignFixture(Module("A", axioms: full), Module("B", axioms: ["propext"]), Module("C"));
        var result = fixture.AlignWithMeasurement(["--add", PathFor("A"), "--add", PathFor("B"), "--add", PathFor("C")],
            response =>
            {
                response[1]!["declarations"]![0]!["strict_omega"]!["axioms"] = new JsonArray("propext");
                response[2]!["strictEligible"] = false;
                response[2]!["declarations"]![0]!.AsObject().Remove("strict_omega");
            });
        Assert.True(result.Success, result.Error);
        var a = Assert.Single(fixture.Load("A").Declarations);
        var b = Assert.Single(fixture.Load("B").Declarations);
        var c = Assert.Single(fixture.Load("C").Declarations);
        Assert.Equal(full.ToArray(), a.Axioms!.Value.ToArray());
        Assert.Equal(["Init", "OmegaMath"], a.Libraries!.Value.ToArray());
        Assert.Equal(["OmegaInit", "OmegaMath"], a.StrictOmega!.Libraries.ToArray());
        Assert.Empty(a.StrictOmega.Axioms);
        Assert.NotEqual(a.StrictOmega.OfficialStatementTypeAddress, a.StrictOmega.StatementTypeAddress);
        Assert.Equal(["propext"], b.StrictOmega!.Axioms.ToArray());
        Assert.Null(c.StrictOmega);
        Assert.Empty(c.Axioms!.Value);

        var before = fixture.Image();
        var ordinary = fixture.Align("--selector", PathFor("A"));
        Assert.True(ordinary.Success, ordinary.Error);
        var retry = fixture.AlignWithMeasurement(["--refresh-annotations", "--selector", PathFor("A")]);
        Assert.True(retry.Success, retry.Error);
        Assert.Contains("changed=0", retry.Output, StringComparison.Ordinal);
        Assert.Equal(before, fixture.Image());
    }

    [Theory]
    [InlineData("official-derived-with-strict")]
    [InlineData("official-derived-ineligible")]
    [InlineData("official-derived-false")]
    [InlineData("official-derived-string")]
    [InlineData("eligibility")]
    [InlineData("eligible-mathlib-only")]
    [InlineData("missing")]
    [InlineData("ineligible")]
    [InlineData("identity")]
    [InlineData("stale")]
    [InlineData("crossed")]
    [InlineData("official-axioms")]
    public void InvalidCompiledResponseDoesNotWriteAnySelectedRecord(string defect)
    {
        using var fixture = new AlignFixture(Module("A"), Module("B"));
        var result = fixture.AlignWithMeasurement(["--add", PathFor("A"), "--add", PathFor("B")], response =>
        {
            var module = response[1]!.AsObject();
            var row = module["declarations"]![0]!.AsObject();
            var strict = row["strict_omega"]!.AsObject();
            switch (defect)
            {
                case "official-derived-with-strict": row["officialDerivedOnly"] = true; break;
                case "official-derived-ineligible":
                    module["strictEligible"] = false;
                    row.Remove("strict_omega");
                    row["officialDerivedOnly"] = true;
                    break;
                case "official-derived-false":
                    row.Remove("strict_omega");
                    row["officialDerivedOnly"] = false;
                    break;
                case "official-derived-string":
                    row.Remove("strict_omega");
                    row["officialDerivedOnly"] = "true";
                    break;
                case "eligible-mathlib-only": module["mathlibOnly"] = new JsonArray(module["module"]!.GetValue<string>()); break;
                case "eligibility": module.Remove("strictEligible"); break;
                case "missing": row.Remove("strict_omega"); break;
                case "ineligible": module["strictEligible"] = false; break;
                case "identity": strict["declaration_name_key"] = "ns(n0,1:a)"; break;
                case "stale": strict["official_statement_type_address"] = Sha256("previous official statement"); break;
                case "crossed": row["statementTypeAddress"] = strict["statement_type_address"]!.GetValue<string>(); break;
                case "official-axioms": row["axioms"] = new JsonArray("propext"); break;
            }
        });
        Assert.False(result.Success);
        Assert.Contains("LEDGER_ALIGN_FAILED", result.Error, StringComparison.Ordinal);
        Assert.Empty(fixture.Image());
    }

    [Fact]
    public void CertifiedOfficialGeneratedRowRetainsOfficialEvidence()
    {
        using var fixture = new AlignFixture(Module("A"), Module("B", axioms: ["propext"]));
        var result = fixture.AlignWithMeasurement(["--add", PathFor("A"), "--add", PathFor("B")], response =>
        {
            var row = response[1]!["declarations"]![0]!.AsObject();
            row.Remove("strict_omega");
            row["officialDerivedOnly"] = true;
        });
        Assert.True(result.Success, result.Error);
        Assert.NotNull(Assert.Single(fixture.Load("A").Declarations).StrictOmega);
        var derived = Assert.Single(fixture.Load("B").Declarations);
        Assert.Null(derived.StrictOmega);
        Assert.Equal(["propext"], derived.Axioms!.Value.ToArray());
        Assert.Equal(["Init", "OmegaMath"], derived.Libraries!.Value.ToArray());
    }

    [Fact]
    public void RefreshRejectsLosingAnExistingStrictMeasurement()
    {
        using var fixture = new AlignFixture(Module("A"));
        var initial = fixture.AlignWithMeasurement(["--add", PathFor("A")]);
        Assert.True(initial.Success, initial.Error);
        var before = fixture.Image();
        var refresh = fixture.AlignWithMeasurement(["--refresh-annotations", "--selector", PathFor("A")], response =>
        {
            response[0]!["strictEligible"] = false;
            response[0]!["declarations"]![0]!.AsObject().Remove("strict_omega");
        });
        Assert.False(refresh.Success);
        Assert.Contains("discard strict_omega measurement", refresh.Error, StringComparison.Ordinal);
        Assert.Equal(before, fixture.Image());
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void RefreshCanClearPairedRowsForCompiledMathlibOnlyPath(bool inherited)
    {
        var owner = inherited ? Module("A", source: "import D5.S0.Carrier.B\ntheorem a : True := by trivial\n", imports: ["B"]) : Module("A");
        using var fixture = new AlignFixture(owner, Module("B"));
        Assert.True(fixture.AlignWithMeasurement(["--add", PathFor("A")]).Success);
        var old = fixture.Load("A");
        var refresh = fixture.AlignWithMeasurement(["--refresh-annotations", "--selector", PathFor("A")], response =>
        {
            var module = response[0]!;
            var name = module["module"]!.GetValue<string>();
            module["strictEligible"] = false;
            module["mathlibOnly"] = inherited ? new JsonArray(name, "D5.S0.Carrier.B") : new JsonArray(name);
            module["declarations"]![0]!.AsObject().Remove("strict_omega");
        });
        Assert.True(refresh.Success, refresh.Error);
        var current = fixture.Load("A");
        Assert.Equal(old.StatementId, current.StatementId);
        Assert.Null(Assert.Single(current.Declarations).StrictOmega);
        Assert.DoesNotContain("mathlibOnly", File.ReadAllText(fixture.StateFile("A")), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("[]")]
    [InlineData("[17]")]
    [InlineData("""["D5.Other"]""")]
    [InlineData("""["D5.S0.Carrier.A", "D5.Unrelated"]""")]
    [InlineData("""["D5.S0.Carrier.A", "D5.S0.Carrier.A"]""")]
    [InlineData("""["D5.S0.Carrier.A", "D5..Child"]""")]
    [InlineData("false")]
    public void RefreshRejectsInvalidCompiledMathlibOnlyPath(string reason)
    {
        using var fixture = new AlignFixture(Module("A"));
        Assert.True(fixture.AlignWithMeasurement(["--add", PathFor("A")]).Success);
        var before = fixture.Image();
        var refresh = fixture.AlignWithMeasurement(["--refresh-annotations", "--selector", PathFor("A")], response =>
        {
            response[0]!["strictEligible"] = false;
            response[0]!["mathlibOnly"] = JsonNode.Parse(reason);
            response[0]!["declarations"]![0]!.AsObject().Remove("strict_omega");
        });
        Assert.False(refresh.Success);
        Assert.Contains("invalid compiled mathlibOnly path", refresh.Error, StringComparison.Ordinal);
        Assert.Equal(before, fixture.Image());
    }

    [Fact]
    public void OrdinaryRepinRemeasuresAnExistingStrictPair()
    {
        var baseline = Module("A");
        using var first = new AlignFixture(baseline);
        var initial = first.AlignWithMeasurement(["--add", PathFor("A")]);
        Assert.True(initial.Success, initial.Error);
        using var next = new AlignFixture(baseline with { StatementMaterial = "True = True" });
        Directory.CreateDirectory(Path.GetDirectoryName(next.StateFile("A"))!);
        File.Copy(first.StateFile("A"), next.StateFile("A"));
        var repin = next.AlignWithMeasurement(["--selector", PathFor("A")]);
        Assert.True(repin.Success, repin.Error);
        var prior = first.Load("A");
        var current = next.Load("A");
        Assert.NotEqual(prior.StatementId, current.StatementId);
        Assert.NotEqual(Assert.Single(prior.Declarations).StrictOmega!.OfficialStatementTypeAddress,
            Assert.Single(current.Declarations).StrictOmega!.OfficialStatementTypeAddress);
    }

    [Fact]
    public void RefreshRejectsMissingMembershipAndChangedPinBeforeWriting()
    {
        var baseline = Module("A");
        var changed = baseline with { StatementMaterial = "True = True" };
        using var fixture = new AlignFixture(changed, Module("B"));
        fixture.Install(BuildCatalog(baseline));
        var before = fixture.Image();
        var missing = fixture.Align("--refresh-annotations", "--selector", PathFor("B"));
        Assert.Contains("not a registered frozen-state member", missing.Error, StringComparison.Ordinal);
        var mismatch = fixture.Align("--refresh-annotations", "--selector", PathFor("A"));
        Assert.Contains("pin or declaration inventory does not match", mismatch.Error, StringComparison.Ordinal);
        Assert.Equal(before, fixture.Image());
    }

    [Theory]
    [InlineData("source", "source hash does not match D5/S0/Carrier/A.lean")]
    [InlineData("material", "Lean statement material hash mismatch")]
    [InlineData("source-imports", "imports do not match source")]
    [InlineData("statement", "statement identity does not match material")]
    [InlineData("axioms", "expected Closed")]
    public void ScopedBundleRetainsSelectedValidationBeforeAnyWrite(string defect, string expectedError)
    {
        using var fixture = new AlignFixture(Module("A"), Module("C") with { StatementMaterial = "True = True" });
        var path = fixture.ReportPath(scoped: true, "--add", PathFor("A"));
        fixture.Corrupt(path, "A", defect);

        AssertRejectedBeforeAnyWrite(fixture, path, expectedError);
    }

    [Theory]
    [InlineData("missing")]
    [InlineData("open")]
    [InlineData("unregistered")]
    [InlineData("duplicate")]
    public void InvalidSelectionWritesNothing(string mode)
    {
        using var fixture = new AlignFixture(mode == "open" ? Module("A", axioms: ["sorryAx"]) : Module("A"));
        var result = mode switch
        {
            "missing" => fixture.Align("--add", PathFor("Missing")),
            "open" => fixture.Align("--add", PathFor("A")),
            "unregistered" => fixture.Align("--selector", PathFor("A")),
            _ => fixture.Align("--add", PathFor("A"), "--add", PathFor("A")),
        };
        Assert.False(result.Success); Assert.Empty(fixture.Image());
    }

    [Fact]
    public void OldStateSchemaFailsClosedBeforeAnyWrite()
    {
        using var fixture = new AlignFixture(Module("A"));
        fixture.Install(BuildCatalog(Module("A")));
        File.WriteAllText(fixture.StateFile("A"), "{\"statement_id\":\"" + Sha256("old pin") + "\"}\n");
        var before = fixture.Image();
        Assert.False(fixture.Align("--selector", PathFor("A")).Success);
        Assert.Equal(before, fixture.Image());
    }

    [Fact]
    public void RetirementKeepsOtherMembersAndRequiresSourceAbsence()
    {
        using var fixture = new AlignFixture(Module("B"));
        fixture.Install(BuildCatalog(Module("A"), Module("B")));
        var before = File.ReadAllBytes(fixture.StateFile("B"));
        Assert.False(fixture.Align("--retire-registration", PathFor("B")).Success);
        var result = fixture.Align("--retire-registration", PathFor("A"));
        Assert.True(result.Success, result.Error);
        Assert.False(File.Exists(fixture.StateFile("A")));
        Assert.Equal(before, File.ReadAllBytes(fixture.StateFile("B")));
    }

    [Fact]
    public void CombinedAddAndRetirementChecksImportsInUnselectedModules()
    {
        var c = Module("C", source: "import D5.S0.Carrier.A\ntheorem c : True := by trivial\n", imports: ["A"]);
        using var fixture = new AlignFixture(Module("B"), c);
        fixture.Install(BuildCatalog(Module("A")));
        var before = fixture.Image();

        var result = fixture.Align("--add", PathFor("B"), "--retire-registration", PathFor("A"));

        Assert.False(result.Success);
        Assert.Contains("still imports retired registration", result.Error, StringComparison.Ordinal);
        Assert.Equal(before, fixture.Image());
    }

    [Theory]
    [InlineData("--from-accepted")]
    [InlineData("--retire-registration")]
    public void RetiredOrIncompleteArgumentsAreRejectedBeforeReads(string argument)
    {
        using var fixture = new AlignFixture();
        Assert.False(fixture.Invoke([argument]).Success);
        Assert.Equal(0, fixture.Repository.ReadCount);
    }

    private static void AssertRejectedBeforeAnyWrite(AlignFixture fixture, string path, string expectedError)
    {
        var result = fixture.Invoke(["--add", PathFor("A"), "--candidate-lean-report", path]);
        Assert.False(result.Success);
        Assert.Contains(expectedError, result.Error, StringComparison.Ordinal);
        Assert.Empty(fixture.Image());
    }

    private sealed class AlignFixture : IDisposable
    {
        private readonly TemporaryDirectory temporary = new();
        private readonly RepositorySnapshot snapshot;
        private readonly LeanAxiomReport report;
        private readonly string fullReport;
        internal FakeRepositoryGateway Repository { get; }
        internal AlignFixture(params ModuleSpec[] modules)
        {
            var files = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["lean-toolchain"] = "leanprover/lean4:v4.24.0\n", ["lake-manifest.json"] = "{}\n",
            };
            foreach (var module in modules) files[PathFor(module.Name)] = module.Source;
            var raw = RawRepositorySnapshot.Create(files.Select(static pair => RawRepositoryEntry.FromText(pair.Key, pair.Value)));
            snapshot = Assert.IsType<SnapshotDecodeOutcome.Decoded>(SnapshotDecoder.Decode(raw)).Snapshot;
            report = LeanAxiomReport.Create(Report(modules).Files.ToDictionary(
                static item => item.Key.Value, item => item.Value with
                { Imports = LeanSourceCatalog.ParseFileImports(snapshot.Files[item.Key], includeImplicitInit: true) }));
            fullReport = Path.Combine(temporary.Path, "full.json");
            RawLeanReportArtifact.WriteFile(fullReport, snapshot, report);
            Repository = new FakeRepositoryGateway(RawChangeSet.Create([]), raw, null);
        }
        internal CommandResult Invoke(string[] arguments) => DagLedgerAlignWriter.Align(temporary.Path, Repository, arguments,
            static (_, reports, records) =>
            {
                foreach (var (path, report) in reports)
                {
                    var axioms = report.Declarations.ToDictionary(static declaration => declaration.NameKey,
                        static declaration => declaration.Axioms);
                    var record = records[path];
                    records[path] = record with
                    {
                        Declarations = record.Declarations.Select(declaration => declaration with
                        {
                            Libraries = [],
                            Axioms = axioms[declaration.DeclarationNameKey],
                        }).ToImmutableArray(),
                    };
                }
                return ImmutableHashSet<RepoPath>.Empty;
            });
        internal CommandResult Align(params string[] options) =>
            Invoke(options.Any(static option => option is "--selector" or "--add")
                ? [.. options, "--candidate-lean-report", ReportPath(scoped: true, options)] : options);
        internal CommandResult AlignWithMeasurement(string[] options, Action<JsonArray>? modify = null)
        {
            var selected = options.Select((option, index) => (option, index))
                .Where(static item => item.option is "--selector" or "--add")
                .Select(item => RepoPath.CreateKnown(options[item.index + 1]));
            var response = JsonSerializer.SerializeToNode(selected.Select(path => new
            {
                module = LeanImportClosure.ModuleName(path),
                strictEligible = true,
                declarations = report.Files[path].Declarations.Select(declaration => new
                {
                    nameKey = declaration.NameKey,
                    kind = declaration.Kind,
                    includeInStatement = declaration.IncludeInStatement,
                    statementTypeAddress = declaration.StatementTypeAddress,
                    libraries = new[] { "OmegaMath", "Init" },
                    axioms = declaration.Axioms,
                    strict_omega = new StrictOmegaMeasurement(declaration.NameKey, declaration.Kind,
                        Sha256("strict:" + declaration.StatementTypeAddress), declaration.StatementTypeAddress,
                        ["OmegaInit", "OmegaMath"], []),
                }),
            }))!.AsArray();
            modify?.Invoke(response);
            File.WriteAllText(Path.Combine(temporary.Path, "measurement-response.json"), response.ToJsonString());
            var script = Path.Combine(temporary.Path, "tools/scripts/agent/frozen-dependencies.sh");
            Directory.CreateDirectory(Path.GetDirectoryName(script)!);
            File.WriteAllText(script, "cp measurement-response.json \"$2\"\n");
            return DagLedgerAlignWriter.Align(temporary.Path, Repository,
                [.. options, "--candidate-lean-report", ReportPath(scoped: true, options)]);
        }
        internal FrozenStateRecord Load(string name)
        {
            var bytes = File.ReadAllBytes(StateFile(name)).ToImmutableArray();
            return FrozenStateRecordLoader.Load(new(FrozenStatePath.FromModulePath(RepoPathFor(name)),
                bytes, Encoding.UTF8.GetString(bytes.AsSpan())));
        }
        internal string ReportPath(bool scoped, params string[] options)
        {
            var targets = options.Select((option, index) => (option, index))
                .Where(static item => item.option is "--selector" or "--add")
                .Select(item => RepoPath.CreateKnown(options[item.index + 1])).ToArray();
            var reportPath = fullReport;
            if (scoped && targets.Length > 0 && targets.All(snapshot.Files.ContainsKey))
            {
                var scope = LeanReportScope.Create(snapshot, targets);
                var selected = RepositorySnapshot.Create(snapshot.Files.Where(item => scope.Paths.Contains(item.Key)).ToImmutableDictionary());
                var selectedReport = LeanAxiomReport.Create(report.Files.Where(item => scope.Paths.Contains(item.Key)).ToDictionary(
                    static item => item.Key.Value, static item => item.Value));
                reportPath = Path.Combine(temporary.Path, "scoped.json");
                RawLeanReportArtifact.WriteFile(reportPath, selected, selectedReport);
                File.WriteAllText(reportPath, File.ReadAllText(reportPath).Replace(RawLeanReportArtifact.Schema, RawLeanReportArtifact.ScopedSchema, StringComparison.Ordinal));
            }
            return reportPath;
        }
        internal void Corrupt(string path, string name, string defect)
        {
            if (defect == "material")
            {
                var address = report.Files[RepoPathFor(name)].Declarations[0].StatementTypeAddress;
                using var archive = ZipFile.Open(RawLeanReportArtifact.MaterialsPath(path), ZipArchiveMode.Update);
                using var stream = archive.GetEntry("sha256/" + address[7..])!.Open();
                stream.SetLength(0);
                stream.Write(Encoding.UTF8.GetBytes("corrupt material"));
                return;
            }
            var document = JsonNode.Parse(File.ReadAllText(path))!;
            var module = document["modules"]!.AsArray().Single(item => item!["source_path"]!.GetValue<string>() == PathFor(name))!;
            switch (defect)
            {
                case "source": module["source_sha256"] = "sha256:" + new string('0', 64); break;
                case "imports": module["imports"] = new JsonArray("Init", "Init"); break;
                case "source-imports": module["imports"] = new JsonArray("Mathlib"); break;
                case "statement": module["declarations"]![0]!["statement_id"] = "sha256:" + new string('0', 64); break;
                case "axioms": module["declarations"]![0]!["axioms"] = new JsonArray("sorryAx"); break;
                default: throw new ArgumentException("unknown fixture defect", nameof(defect));
            }
            File.WriteAllBytes(path, StructuredCanonicalWriter.WriteJson(document.ToJsonString()).ToArray());
        }
        internal void Install(FrozenMaterialCatalog catalog)
        {
            foreach (var file in StateFiles(catalog))
            {
                var path = Path.Combine(temporary.Path, file.Path.Value);
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                File.WriteAllBytes(path, file.RawBytes.AsSpan().ToArray());
            }
        }
        internal string StateFile(string name) => Path.Combine(temporary.Path, FrozenStatePath.FromModulePath(RepoPathFor(name)).Value);
        internal Dictionary<string, string> Image()
        {
            var root = Path.Combine(temporary.Path, FrozenStatePath.Root);
            return Directory.Exists(root) ? Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories)
                .ToDictionary(path => Path.GetRelativePath(temporary.Path, path).Replace('\\', '/'), File.ReadAllText, StringComparer.Ordinal)
                : new(StringComparer.Ordinal);
        }
        public void Dispose() => temporary.Dispose();
    }
}
