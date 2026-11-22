using static StrataLint.TestSupport.TransactionFixture;

namespace StrataLint.Tests;

public sealed class PlaybookWorkflowScriptTests
{
    [Fact]
    public void DepositNeverExecutesBranchMerge()
    {
        if (OperatingSystem.IsWindows()) return;
        using var fixture = new TransactionFixture();
        fixture.ChangeFormalization();
        var result = fixture.Run("deposit");

        Assert.True(result.ExitCode == 0, Diagnostics(result));
        Assert.DoesNotContain(fixture.Calls(),
            call => call.StartsWith("git-branch-merge:", StringComparison.Ordinal));
    }
}
