using System.Text;
using StrataLint.Engine;

namespace StrataLint.Cli;

internal abstract record AdmissionTopologyOutcome
{
    private AdmissionTopologyOutcome() { }

    internal sealed record BootstrapNotActive(string DefaultBranch) : AdmissionTopologyOutcome;

    internal sealed record SteadyStateActive(string DefaultBranch) : AdmissionTopologyOutcome;

    internal sealed record InfrastructureFailure(string Message) : AdmissionTopologyOutcome;
}

internal static class AdmissionWorkflowTopology
{
    internal const string WorkflowPath = ".github/workflows/ci-current.yml";

    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    internal static bool HasRequiredBaseGate(ReadOnlySpan<byte> bytes, string defaultBranch,
        Func<(string? Pin, string? Definition)>? releasedGate = null)
    {
        try
        {
            var caller = CiWorkflowDocument.Parse(StrictUtf8.GetString(bytes));
            if (caller?.RunsOnBranch("pull_request", defaultBranch) != true) return false;
            if (caller.CalledGateRevision is null) return caller.HasDeltaStep();
            if (releasedGate is null) return false;
            var (pin, definition) = releasedGate();
            return caller.ResolveCalledGate(pin, definition)?.HasDeltaStep() == true;
        }
        catch (DecoderFallbackException)
        {
            return false;
        }
    }
}
