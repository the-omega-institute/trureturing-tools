using System.Text;
using System.Text.Json;
using StrataLint.TestSupport;

namespace StrataLint.Lean.Tests;

public sealed class LeanReportSelectionTests
{
    [Theory]
    [InlineData("missing")]
    [InlineData("different-path")]
    [InlineData("unregistered")]
    [InlineData("excluded")]
    public void ExecutionToolchainMustBeAnExplicitRequiredInput(string defect)
    {
        if (OperatingSystem.IsWindows()) return;
        var root = TestRepositoryLayout.FindRoot();
        var result = TestProcessRunner.Run("python3", ["-B", "-c", """
            import pathlib, sys
            sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'tools/lean-inspector/tests'))
            from test_reuse import ReuseTests
            import publication
            fixture = ReuseTests()
            fixture.setUp()
            try:
                defect = sys.argv[2]
                if defect == 'missing': del fixture.policy['report_execution']['toolchain']
                if defect == 'different-path': fixture.policy['report_execution']['toolchain'] = 'unregistered/pin'
                if defect == 'unregistered': fixture.policy['config_inputs']['include'] = []
                if defect == 'excluded': fixture.policy['config_inputs']['exclude'] = ['lean-toolchain']
                fixture.write_policy()
                try: publication.selection.Selection(fixture.root).validate('lean-report')
                except ValueError as error:
                    print(error)
                    raise SystemExit(4)
            finally:
                fixture.doCleanups()
            """, root, defect], root, TestBudgets.WorkflowProcessHangGuard, 1024 * 1024);
        Assert.Equal(4, result.ExitCode);
        Assert.Contains("lean-report-inputs.json", Encoding.UTF8.GetString(result.StandardOutput), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("registration_failures")]
    [InlineData("glob_semantics")]
    [InlineData("reg_module_name")]
    [InlineData("source_and_policy_identity")]
    [InlineData("source_traversal_io")]
    public void RegisteredSelectionContract(string scenario)
    {
        if (OperatingSystem.IsWindows()) return;
        using var temporary = new TemporaryDirectory();
        var root = TestRepositoryLayout.FindRoot();
        var result = TestProcessRunner.Run("python3",
            ["-B", Path.Combine(root, "tools/tests/StrataLint.Lean.Tests/Native/lean_report_selection_fixture.py"),
                root, temporary.Path, scenario], temporary.Path,
            TestBudgets.WorkflowProcessHangGuard, 1024 * 1024);
        Assert.True(result.ExitCode == 0, Encoding.UTF8.GetString(result.StandardOutput)
            + Encoding.UTF8.GetString(result.StandardError));
    }

}
