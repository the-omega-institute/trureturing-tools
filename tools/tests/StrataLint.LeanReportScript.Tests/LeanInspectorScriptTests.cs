using StrataLint.Runtime;
using StrataLint.Engine;

namespace StrataLint.LeanReportScript.Tests;

public sealed class LeanInspectorScriptTests
{
    [Fact]
    public void RecordsInvocationLocalBuildWork()
    {
        if (OperatingSystem.IsWindows()) return;
        var root = TestRepositoryLayout.FindRoot();
        var result = TestProcessRunner.Run("python3", ["-B",
            Path.Combine(root, "tools/tests/StrataLint.ScriptTests/Fixtures/build_work_contract.py")], root,
            BoundedProcessRunner.HangDetectionBudget, 1024 * 1024);
        Assert.True(result.ExitCode == 0, System.Text.Encoding.UTF8.GetString(result.StandardOutput) +
            System.Text.Encoding.UTF8.GetString(result.StandardError));
    }

    [Fact]
    public void JudgeProducerReuseRequiresARunnableBundleAndPreservesFallback()
    {
        if (OperatingSystem.IsWindows()) return;
        var root = TestRepositoryLayout.FindRoot();
        var result = TestProcessRunner.Run("python3", ["-B",
            Path.Combine(root, "tools/tests/StrataLint.ScriptTests/Fixtures/judge_producer_contract.py")], root,
            BoundedProcessRunner.HangDetectionBudget, 1024 * 1024);
        Assert.True(result.ExitCode == 0, System.Text.Encoding.UTF8.GetString(result.StandardOutput) +
            System.Text.Encoding.UTF8.GetString(result.StandardError));
    }

}
