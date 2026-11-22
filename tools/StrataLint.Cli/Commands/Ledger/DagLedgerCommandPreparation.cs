using System.Collections.Immutable;
using StrataLint.Engine;

namespace StrataLint.Cli;

internal static class DagLedgerCommandPreparation
{
    private static bool IsLedgerInput(string path) => path.EndsWith(".lean", StringComparison.Ordinal)
        || path == RepositoryPathPolicy.AssumptionRegistryPath
        || FrozenStatePath.IsUnderRoot(path)
        || path is "lean-toolchain" or "lakefile.toml" or "lake-manifest.json" or "lean-report-inputs.json";

    internal static TruthContext BuildTruthScoped(
        IRepositoryGateway repository,
        ILeanReportSource leanReportSource,
        IReadOnlyCollection<RepoPath> requestedTargets,
        bool requireAllSources = false)
    {
        var inventory = Decode(Ask(() => repository.ReadCurrentProjection(path =>
            IsLedgerInput(path)
                && (requireAllSources || !path.EndsWith(".lean", StringComparison.Ordinal)))));
        var scope = LeanReportScope.Create(inventory, requestedTargets, path =>
        {
            if (inventory.Files[path].ContentWasRead) return inventory.Files[path];
            var source = Decode(Ask(() => repository.ReadCurrent([":(literal)" + path.Value])));
            return source.Files.TryGetValue(path, out var file) ? file
                : throw new InvalidOperationException("scoped Lean report source bytes are unavailable: " + path.Value);
        });
        var report = leanReportSource.Load(scope);
        var snapshot = scope.SourceSnapshot;
        return new(snapshot, ValidateLean(snapshot, report), report);
    }

    /// Runs a gateway call that reads repository state, marking anything it throws. A repository
    /// that cannot be read is a fault in the environment, not a statement about the frozen ledger,
    /// and callers classifying failures cannot tell the two apart otherwise -- both arrive as
    /// IOException or InvalidOperationException. Reference validation is not routed through here:
    /// its refusals are about the ledger's contents.
    internal static T Ask<T>(Func<T> gatewayCall)
    {
        try
        {
            return gatewayCall();
        }
        catch (Exception exception) when (exception is not RepositoryUnavailableException)
        {
            throw new RepositoryUnavailableException(exception);
        }
    }

    /// Raised when a gateway call fails. Never escapes a classifying caller: the original is
    /// rethrown in its place.
    internal sealed class RepositoryUnavailableException(Exception inner)
        : Exception("repository could not be read", inner);

    internal sealed class FileLeanReportSource(string path) : ILeanReportSource
    {
        public LeanAxiomReport Load(LeanReportScope scope) =>
            RawLeanReportArtifact.ReadFileForScope(path, scope, validateMaterials: true);
    }

    internal static RepositorySnapshot ReadRetirementSources(IRepositoryGateway repository) =>
        Decode(Ask(() => repository.ReadCurrentProjection(IsLedgerInput)));

    /// Bytes that will not decode are a fault in the repository we were handed, not a verdict about
    /// its ledger -- spec A16 classifies snapshot rejection as infrastructure. The read itself can
    /// succeed here, so marking only the gateway call would leave this failure to arrive as an
    /// ordinary InvalidOperationException and be reported as a ledger fault. The decode message is
    /// kept as the inner exception so callers can still name the cause.
    private static RepositorySnapshot Decode(RawRepositorySnapshot raw) =>
        SnapshotDecoder.Decode(raw) switch
        {
            SnapshotDecodeOutcome.Decoded decoded => decoded.Snapshot,
            SnapshotDecodeOutcome.InfrastructureFailure failure =>
                throw new RepositoryUnavailableException(new InvalidOperationException(failure.Message)),
        };

    private static AcceptedLeanClosure ValidateLean(
        RepositorySnapshot snapshot,
        LeanAxiomReport report) =>
        LeanClosureValidator.Validate(snapshot, report) switch
        {
            LeanValidationOutcome.Accepted accepted => accepted.Capability,
            LeanValidationOutcome.InfrastructureFailure failure =>
                throw new InvalidOperationException(failure.Message),
        };

}
