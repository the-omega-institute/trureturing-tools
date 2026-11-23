using System.Collections.Immutable;
using System.Text.Json;
using StrataLint.Engine;
using StrataLint.Scribe;

namespace StrataLint.Cli;

internal sealed partial class ProductionCliEnvironment
{
    public ExplicitCommandResult CheckCurrent(IReadOnlyList<string> arguments) =>
        CheckStage(() => ParseCheckArguments(arguments), delta: false, new CheckInputs());

    public ExplicitCommandResult CheckDelta(IReadOnlyList<string> arguments) =>
        CheckStage(() => ParseCheckArguments(arguments), delta: true, new CheckInputs());

    public ExplicitCommandResult CheckCurrentAndDelta(
        IReadOnlyList<string> arguments, Action<ExplicitCommandResult> writeCurrent)
    {
        CheckArguments options;
        try
        {
            options = ParseCheckArguments(arguments);
            if (options.ProtectedBase is null || options.ChangedPaths is null)
                throw CheckUsage();
        }
        catch (InvalidOperationException)
        {
            return new(2, "", "INFRASTRUCTURE_FAILURE USAGE: check-current-and-delta --protected-base SHA "
                + "[--scope-base SHA] --changed-paths-from FILE [--scoped-reports-from FILE]\n");
        }

        var inputs = new CheckInputs();
        writeCurrent(CheckStage(() => options with { ProtectedBase = null }, delta: false, inputs));
        return CheckStage(() => options with { ScopeBase = null }, delta: true, inputs);
    }

    private ExplicitCommandResult CheckStage(Func<CheckArguments> parseOptions, bool delta, CheckInputs inputs)
    {
        var timing = new AdmissionCheckTiming(timeProvider);
        ImmutableArray<Diagnostic> planeObservations = [];
        try
        {
            var options = parseOptions();
            if (delta != (options.ProtectedBase is not null) || options.ChangedPaths is null
                || options.ScopeBase is not null && delta)
                throw CheckUsage();

            var raw = inputs.Raw ??= timing.Measure("repository-read", () => AdmissionRepositoryInputs.ReadCurrent(repository));
            var prepared = delta ? timing.Measure("repository-prepare", () => repository.Prepare(options.ProtectedBase)) : null;
            var baselineRaw = prepared is null ? null : timing.Measure("repository-read-base",
                () => AdmissionRepositoryInputs.ReadBaseline(repository, prepared.Revision));
            var observedPlane = ImmutableArray<Diagnostic>.Empty;
            var planeFailure = prepared is null ? null
                : timing.Measure("admission-plane", () => EvaluateAdmissionPlane(raw, baselineRaw!, prepared.Changes, out observedPlane));
            planeObservations = observedPlane;
            if (planeFailure is not null)
                return new(2, RenderPlaneObservations(planeObservations), planeFailure.Message + "\n");

            var current = inputs.Snapshot ??= timing.Measure("snapshot-load", () => Decode(raw));
            var scopeBase = options.ScopeBase is null ? prepared
                : timing.Measure("scope-base", () => repository.Prepare(options.ScopeBase));
            var changes = ScopedAdmissionReports.ChangedPaths(repositoryRoot, options.ChangedPaths, scopeBase);
            var plan = timing.Measure("lean-scope", () => PullRequestLeanChecks.CreatePlan(
                repositoryRoot, current, changes));
            var report = inputs.ReadReport(repositoryRoot, current, plan.Targets, options.ScopedReports, timing);
            var policy = timing.Measure("policy-load", () => RepositoryPolicyLoader.Load(current) switch
            {
                PolicyLoadOutcome.Accepted accepted => accepted.Policy,
                PolicyLoadOutcome.InfrastructureFailure failure => throw new InvalidDataException(failure.Message),
            });
            var lean = timing.Measure("lean-closure", () => LeanClosureValidator.Validate(current, report) switch
            {
                LeanValidationOutcome.Accepted accepted => accepted.Capability,
                LeanValidationOutcome.InfrastructureFailure failure => throw new InvalidDataException(failure.Message),
            });

            RuleExecutionOutcome result;
            if (prepared is not null)
            {
                var baseline = timing.Measure("snapshot-load-base", () => Decode(baselineRaw!));
                var topology = timing.Measure("test-topology", () => RepositoryRules.EvaluateSnapshots(baseline, current), static outcome => !outcome.IsAccepted);
                if (!topology.IsAccepted)
                    return new(1, RenderPlaneObservations(planeObservations) + "TEST_PROJECT_TOPOLOGY " + topology.Message + "\n", "");
                var meta = BootstrapGate.Evaluate(prepared.Changes) switch
                {
                    BootstrapOutcome.Clear clear => MetaEvaluationProfile.ForClear(clear.Capability),
                    BootstrapOutcome.ProtectedSurfaceVerificationRequired change => MetaEvaluationProfile.ForProtectedSurface(change.ChangeSet),
                    BootstrapOutcome.InfrastructureFailure failure => throw new InvalidDataException(failure.Message),
                };
                result = timing.Measure("rule-passes", () => AdmissionPipeline.CheckDelta(
                    DeltaRuleContext.Create(current, baseline, policy, lean, prepared.Changes, meta,
                        ruleInputChanges: PullRequestLeanChecks.SemanticChanges(prepared.Changes)),
                    MeasureRule), Blocked);
            }
            else
            {
                var verified = timing.Measure("scribe-verify", () =>
                    scribeEmissionVerifier?.VerifyScoped(current, report, changes, plan.Paths));
                result = timing.Measure("rule-passes", () => AdmissionPipeline.CheckCurrent(
                    CurrentRuleContext.Create(current, policy, lean, verified), MeasureRule), Blocked);
                if (timing.Measure("canonicalization", () => RepositoryCanonicalizer.Validate(current, policy),
                        static outcome => outcome is CanonicalizationOutcome.InfrastructureFailure) is CanonicalizationOutcome.InfrastructureFailure failure)
                    return new(2, RenderStage(result, planeObservations).Output, "INFRASTRUCTURE_FAILURE " + failure.Message + "\n");
            }
            return RenderStage(result, planeObservations);
        }
        catch (ScribeVerificationException exception)
        {
            return new(exception.ExitCode, RenderPlaneObservations(planeObservations),
                (exception.ExitCode == 2 ? "INFRASTRUCTURE_FAILURE " : "") + exception.Message + "\n");
        }
        catch (Exception exception)
        {
            return new(2, RenderPlaneObservations(planeObservations), "INFRASTRUCTURE_FAILURE " + exception.Message + "\n");
        }

        ImmutableArray<RuleFinding> MeasureRule(RuleId ruleId, AdmissionEffect admissionEffect, Func<ImmutableArray<RuleFinding>> evaluate) =>
            timing.Measure("rule-" + ruleId.Value.ToLowerInvariant(), evaluate,
                findings => findings.Any(finding => (finding.Effect ?? admissionEffect) is AdmissionEffect.Block));
    }

    private sealed class CheckInputs
    {
        internal RawRepositorySnapshot? Raw;
        internal RepositorySnapshot? Snapshot;
        private (string? Manifest, ImmutableArray<string> Targets, LeanAxiomReport Report)? verifiedReport;

        internal LeanAxiomReport ReadReport(string root, RepositorySnapshot snapshot,
            ImmutableArray<string> targets, string? manifest, AdmissionCheckTiming timing)
        {
            // Both phases validate their changed paths and derive their scope before reusing evidence.
            if (verifiedReport is { } verified && verified.Manifest == manifest && verified.Targets.SequenceEqual(targets))
                return verified.Report;

            var report = timing.Measure("lean-report-load", () => ScopedAdmissionReports.Read(root, snapshot, targets, manifest));
            verifiedReport = (manifest, targets, report);
            return report;
        }
    }

    private static string RenderPlaneObservations(ImmutableArray<Diagnostic> observations) =>
        observations.IsDefaultOrEmpty ? "" : JsonSerializer.Serialize(new { diagnostics = observations }) + "\n";

    private static ExplicitCommandResult RenderStage(RuleExecutionOutcome result,
        ImmutableArray<Diagnostic> planeObservations = default)
    {
        if (result is RuleExecutionOutcome.InfrastructureFailure failure)
            return new(2, RenderPlaneObservations(planeObservations), failure.Message + "\n");
        var rules = ((RuleExecutionOutcome.Completed)result).Capability;
        var blocked = rules.Diagnostics.Any(d => d.AdmissionEffect is AdmissionEffect.Block
            || d.AdmissionEffect is AdmissionEffect.HumanGate && d.RuleId != RuleId.CreateKnown(22));
        var annotated = rules.Diagnostics.Any(d => d.RuleId == RuleId.CreateKnown(22) && d.AdmissionEffect is AdmissionEffect.HumanGate);
        return new(blocked ? 1 : annotated ? 3 : 0, JsonSerializer.Serialize(new
        {
            executed = rules.ExecutedRules.Select(id => id.Value),
            skipped = rules.SkippedRules.Select(id => id.Value),
            deferred = rules.DeferredRules,
            diagnostics = planeObservations.IsDefaultOrEmpty ? rules.Diagnostics : rules.Diagnostics.AddRange(planeObservations),
        }) + "\n", "");
    }

    private static bool Blocked(RuleExecutionOutcome outcome) =>
        outcome is not RuleExecutionOutcome.Completed completed
        || completed.Capability.Diagnostics.Any(diagnostic => diagnostic.AdmissionEffect is AdmissionEffect.Block);
}
