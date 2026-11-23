using System.Collections.Immutable;
using System.Text;
using System.Text.Json;
using StrataLint.Engine;

namespace StrataLint.Cli;

internal static class DagLedgerAlignWriter
{
    internal static CommandResult Align(string root, IRepositoryGateway repository, IReadOnlyList<string> arguments,
        FrozenDependencyMeasurement.Measure? measure = null)
    {
        try
        {
            var options = ParseArguments(arguments);
            var selected = options.Selectors.Concat(options.Adds).Distinct().OrderBy(static path => path.Value, StringComparer.Ordinal).ToArray();
            var truth = selected.Length > 0
                ? DagLedgerCommandPreparation.BuildTruthScoped(repository,
                    new DagLedgerCommandPreparation.FileLeanReportSource(options.ReportPath!), selected,
                    requireAllSources: !options.Retirements.IsEmpty)
                : null;
            var snapshot = truth?.Snapshot ?? DagLedgerCommandPreparation.ReadRetirementSources(repository);
            var states = truth is null ? null : LeanTruthStates.Resolve(snapshot, truth.Lean);
            var planned = new Dictionary<RepoPath, FrozenStateRecord>();
            var measurementReports = new Dictionary<RepoPath, LeanFileReport>();
            var backups = new Dictionary<RepoPath, ImmutableArray<byte>?>();
            foreach (var path in selected)
            {
                var current = FrozenStateWriter.ReadCurrentBytes(root, path);
                backups.Add(path, current);
                if (options.Selectors.Contains(path) && current is null)
                    throw new InvalidOperationException($"selector {path.Value} is not a registered frozen-state member");
                var existing = current is { } bytes ? LoadState(path, bytes) : null;
                if (!snapshot.Files.ContainsKey(path)) throw new InvalidOperationException($"module {path.Value} does not exist");
                if (!states!.TryGetValue(path, out var state) || state is not TruthState.Closed)
                    throw new InvalidOperationException($"module {path.Value} has TruthState={state}, expected Closed");
                if (!truth!.Report.Files.TryGetValue(path, out var report))
                    throw new InvalidOperationException($"module {path.Value} has no current Lean report");
                var record = FrozenStateRecord.FromReport(path, report);
                if (options.RefreshAnnotations)
                {
                    if (existing!.StatementId != record.StatementId || !existing.HasInventory(record.Declarations))
                        throw new InvalidOperationException($"selector {path.Value} pin or declaration inventory does not match the current report");
                    record = existing;
                }
                else if (existing is not null && existing.StatementId == record.StatementId)
                {
                    var previous = existing.Declarations.ToDictionary(static declaration => declaration.Identity);
                    record = record with
                    {
                        Declarations = record.Declarations.Select(declaration =>
                            previous.GetValueOrDefault(declaration.Identity, declaration)).ToImmutableArray(),
                    };
                }
                planned.Add(path, record);
                if (current is null || options.RefreshAnnotations) measurementReports.Add(path, report);
            }
            if (!options.Retirements.IsEmpty)
            {
                foreach (var file in Directory.EnumerateFiles(Path.Combine(root, FrozenStatePath.Root), "*", SearchOption.AllDirectories))
                {
                    var statePath = Path.GetRelativePath(root, file).Replace('\\', '/');
                    if (!FrozenStatePath.TryToModulePath(statePath, out var module)) throw new FormatException("invalid frozen state path: " + statePath);
                    if (!snapshot.Files.ContainsKey(module) && !options.Retirements.Contains(module))
                        throw new InvalidOperationException($"module {module.Value} does not exist; missing registrations require explicit --retire-registration");
                }
            }
            foreach (var retired in options.Retirements)
            {
                if (!retired.Value.StartsWith("D5/", StringComparison.Ordinal))
                    throw new InvalidOperationException($"registration retirement requires a D5 module: {retired.Value}");
                if (snapshot.Files.ContainsKey(retired)) throw new InvalidOperationException($"cannot retire existing module {retired.Value}");
                var pin = FrozenStateWriter.ReadCurrentBytes(root, retired)
                    ?? throw new InvalidOperationException($"selector {retired.Value} is not a registered frozen-state member");
                _ = LoadState(retired, pin);
                backups.Add(retired, pin);
                foreach (var file in snapshot.Files.Values.Where(static file => LeanClosureValidator.IsReportLean(file.Path.Value)))
                    if (LeanSourceCatalog.ParseFileImports(file).Contains(LeanImportClosure.ModuleName(retired)))
                        throw new InvalidOperationException($"module {file.Path.Value} still imports retired registration {retired.Value}");
            }
            (measure ?? FrozenDependencyMeasurement.Attach)(root, measurementReports, planned);
            using var writeGuard = FrozenStateWriter.AcquireWriteGuard(root);
            foreach (var (path, previous) in backups)
            {
                var actual = FrozenStateWriter.ReadCurrentBytes(root, path);
                if (actual.HasValue != previous.HasValue
                    || actual.HasValue && !actual.Value.AsSpan().SequenceEqual(previous!.Value.AsSpan()))
                    throw new InvalidOperationException($"selector {path.Value} changed while preparing the write");
            }
            var changed = 0;
            var added = 0;
            var written = new List<RepoPath>();
            try
            {
                foreach (var (path, record) in planned)
                    if (FrozenStateWriter.Write(root, path, record.StatementId, record.Declarations))
                    {
                        written.Add(path);
                        if (backups[path] is null) added++; else changed++;
                    }
                foreach (var path in options.Retirements)
                    if (FrozenStateWriter.Delete(root, path)) written.Add(path);
            }
            catch
            {
                foreach (var path in written) FrozenStateWriter.Restore(root, path, backups[path]);
                throw;
            }
            return new(true, Summary(selected.Length, changed, added, selected.Length - changed - added)
                + (options.Retirements.IsEmpty ? string.Empty : $"LEDGER_RETIRE registrations={options.Retirements.Length}\n"), string.Empty);
        }
        catch (Exception exception) when (exception is ArgumentException or FormatException or IOException or InvalidDataException
            or InvalidOperationException or JsonException or UnauthorizedAccessException or TimeoutException or KeyNotFoundException
            or DagLedgerCommandPreparation.RepositoryUnavailableException)
        {
            return new(false, Summary(0, 0, 0, 0, 1), "LEDGER_ALIGN_FAILED " + exception.Message
                + (exception.InnerException is null ? string.Empty : " Cause: " + exception.InnerException.Message) + "\n");
        }
    }

    private static FrozenStateRecord LoadState(RepoPath path, ImmutableArray<byte> bytes) =>
        FrozenStateRecordLoader.Load(new(FrozenStatePath.FromModulePath(path), bytes, Encoding.UTF8.GetString(bytes.AsSpan())));

    private static AlignOptions ParseArguments(IReadOnlyList<string> arguments)
    {
        string? report = null;
        var refresh = false;
        var selectors = ImmutableArray.CreateBuilder<RepoPath>();
        var adds = ImmutableArray.CreateBuilder<RepoPath>();
        var retirements = ImmutableArray.CreateBuilder<RepoPath>();
        for (var index = 0; index < arguments.Count; index++)
            switch (arguments[index])
            {
                case "--candidate-lean-report" when ++index < arguments.Count && report is null: report = arguments[index]; break;
                case "--refresh-annotations" when !refresh: refresh = true; break;
                case "--selector" when ++index < arguments.Count: selectors.Add(Module(arguments[index])); break;
                case "--add" when ++index < arguments.Count: adds.Add(Module(arguments[index])); break;
                case "--retire-registration" when ++index < arguments.Count: retirements.Add(Module(arguments[index])); break;
                default: throw Usage();
            }
        var selectionCount = selectors.Count + adds.Count + retirements.Count;
        if (selectionCount == 0 || (selectors.Count + adds.Count > 0 && string.IsNullOrWhiteSpace(report))) throw Usage();
        if (refresh && (adds.Count > 0 || retirements.Count > 0)) throw Usage();
        if (selectors.Concat(adds).Concat(retirements).GroupBy(static path => path).Any(static group => group.Count() != 1))
            throw new InvalidOperationException("duplicate or conflicting module selector");
        return new(report, selectors.ToImmutable(), adds.ToImmutable(), retirements.ToImmutable(), refresh);
    }

    private static RepoPath Module(string value)
    {
        if (!RepoPath.TryCreate(value, out var path)) throw new FormatException("module selector is not canonical: " + value);
        _ = FrozenStatePath.FromModulePath(path);
        return path;
    }
    private static InvalidOperationException Usage() => new("USAGE: StrataLint ledger-align [--candidate-lean-report FILE] "
        + "(--selector MODULE | --add MODULE | --retire-registration MODULE)... "
        + "(report required for --selector or --add). Use --refresh-annotations with --selector to preserve existing pins.");
    private static string Summary(int considered, int changed, int added, int unchanged, int conflicts = 0) =>
        $"LEDGER_ALIGN selectors_considered={considered} changed={changed} added={added} unchanged={unchanged} conflicts={conflicts}\n";
    private sealed record AlignOptions(string? ReportPath, ImmutableArray<RepoPath> Selectors,
        ImmutableArray<RepoPath> Adds, ImmutableArray<RepoPath> Retirements, bool RefreshAnnotations);
}
