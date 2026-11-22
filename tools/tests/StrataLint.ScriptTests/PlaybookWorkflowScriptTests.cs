using StrataLint.Runtime;
using static StrataLint.TestSupport.TransactionFixture;
using System.Text;
using System.Text.Json;
using StrataLint.Engine;

namespace StrataLint.Tests;

public sealed class PlaybookWorkflowScriptTests
{
    private const string ScriptPath = "tools/scripts/workflow/playbook-workflows.sh";
    private const string SyntheticBaseSha = "0000000000000000000000000000000000000001";

    [Theory]
    [InlineData("deposit")]
    public void CanonicalPlaybookVerbNeverExecutesBranchMerge(string command)
    {
        if (OperatingSystem.IsWindows()) return;

        if (command == "deposit")
        {
            using var fixture = new TransactionFixture();
            fixture.ChangeFormalization();
            var result = fixture.Run(command);

            Assert.True(result.ExitCode == 0, Diagnostics(result));
            Assert.DoesNotContain(
                fixture.Calls(),
                call => call.StartsWith("git-branch-merge:", StringComparison.Ordinal));
            return;
        }

        using (var fixture = new PlaybookFixture())
        {
            var result = fixture.Run(command, "synthetic-base");

            Assert.True(result.ExitCode == 0, Diagnostics(result));
            Assert.DoesNotContain(
                fixture.Calls(),
                call => call.StartsWith("git-branch-merge:", StringComparison.Ordinal));
        }
    }

    private static void WriteTruthGraph(PlaybookFixture fixture, string module) =>
        WriteClosedModules(fixture, [module]);

    private static void WriteEmptyTruthGraph(PlaybookFixture fixture) =>
        WriteClosedModules(fixture, []);

    private static void WriteClosedModules(PlaybookFixture fixture, string[] modules)
    {
        var path = Path.Combine(fixture.Temporary.Path, "closed-modules.json");
        ScriptHarnessScratch.EnsureDirectory(Path.GetDirectoryName(path)!);
        ScriptHarnessScratch.WriteScratchText(path, JsonSerializer.Serialize(modules));
    }

    private static bool StateFragmentExists(PlaybookFixture fixture, string module) =>
        new FileInfo(Path.Combine(
            fixture.Temporary.Path,
            "Golden",
            "Frozen",
            "state",
            module + ".json")).Exists;

    private sealed class PlaybookFixture : IDisposable
    {
        private readonly TemporaryDirectory temporary = new();
        private readonly string callsPath;
        private readonly string binPath;

        internal PlaybookFixture()
        {
            var root = TestRepositoryLayout.FindRoot();
            callsPath = Path.Combine(temporary.Path, "calls");
            binPath = Path.Combine(temporary.Path, "bin");
            ScriptHarnessScratch.EnsureDirectory(binPath);
            var scriptTarget = Path.Combine(temporary.Path, ScriptPath);
            ScriptHarnessScratch.CopyScriptInto(Path.Combine(root, ScriptPath), scriptTarget);
            Directory.CreateDirectory(Path.Combine(temporary.Path, "Golden", "Frozen", "state"));
            WriteEmptyTruthGraph(this);
            WriteExecutable("make", "printf 'make:%s\\n' \"$*\" >> \"$PLAYBOOK_TEST_CALLS\"");
            WriteExecutable(
                "git",
                $$"""
                arguments=("$@")
                index=0
                while [[ $index -lt ${#arguments[@]} ]]; do
                  token=${arguments[index]}
                  case "$token" in
                    -C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--config-env)
                      index=$((index + 2))
                      ;;
                    --git-dir=*|--work-tree=*|--namespace=*|--super-prefix=*|--config-env=*)
                      index=$((index + 1))
                      ;;
                    --no-pager|--paginate|--bare|--literal-pathspecs|--no-literal-pathspecs|--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs)
                      index=$((index + 1))
                      ;;
                    --)
                      index=$((index + 1))
                      break
                      ;;
                    -*) index=$((index + 1)) ;;
                    *) break ;;
                  esac
                done
                subcommand=${arguments[index]:-}
                if [[ $subcommand == merge ]]; then
                  printf 'git-branch-merge:%s\n' "${arguments[*]}" >> "$PLAYBOOK_TEST_CALLS"
                  exit 97
                fi
                printf 'git:%s\n' "${arguments[*]}" >> "$PLAYBOOK_TEST_CALLS"
                if [[ $subcommand == rev-parse && ( "${arguments[index+1]:-}" == HEAD^1 || "${arguments[index+1]:-}" == --verify && "${arguments[index+2]:-}" == 'synthetic-base^{commit}' ) ]]; then
                  printf '%s\n' '{{SyntheticBaseSha}}'
                fi
                """);
            WriteExecutable(
                "dotnet",
                "args=\"$*\"; command=${args##* -- }; printf 'dotnet:%s\\n' \"$command\" >> \"$PLAYBOOK_TEST_CALLS\"; "
                + "if [[ -n ${PLAYBOOK_DOTNET_FAILURE:-} && $command == $PLAYBOOK_DOTNET_FAILURE* ]]; then "
                + "printf '%s\\n' \"$PLAYBOOK_DOTNET_DIAGNOSTIC\" >&2; exit 1; fi; "
                + "if [[ $command == 'ledger-align --list-closed '* ]]; then cat closed-modules.json; exit 0; fi; "
                + "read -r -a parts <<< \"$command\"; "
                + "for ((i=1; i<${#parts[@]}; i++)); do "
                + "if [[ ${parts[i]} == --add ]]; then module=${parts[i+1]}; "
                + "state=Golden/Frozen/state/${module}.json; mkdir -p \"$(dirname \"$state\")\"; "
                + "printf '{\"statement_id\":\"sha256:%064d\",\"declarations\":[]}\\n' 1 > \"$state\"; "
                + "fi; done");
        }

        internal TemporaryDirectory Temporary => temporary;

        internal ProcessOutput Run(
            string command,
            string baseline,
            string? dotnetFailure = null,
            string? dotnetDiagnostic = null) =>
            TestProcessRunner.Run(
                "/usr/bin/env",
                [
                    $"PATH={binPath}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}",
                    $"PLAYBOOK_TEST_CALLS={callsPath}",
                    $"PLAYBOOK_DOTNET_FAILURE={dotnetFailure}",
                    $"PLAYBOOK_DOTNET_DIAGNOSTIC={dotnetDiagnostic}",
                    "/bin/bash",
                    Path.Combine(temporary.Path, ScriptPath),
                    command,
                    baseline,
                ],
                temporary.Path,
                BoundedProcessRunner.HangDetectionBudget,
                64 * 1024);

        internal string[] Calls() => ScriptHarnessScratch.ReadRecordedCalls(callsPath);

        private void WriteExecutable(string name, string body)
        {
            if (OperatingSystem.IsWindows()) return;
            ScriptHarnessScratch.WriteExecutableStub(Path.Combine(binPath, name), body);
        }

        public void Dispose() => temporary.Dispose();
    }

}
