using System.Text.Json;
using StrataLint.TestSupport;
using Xunit;
using FactAttribute = Xunit.SkippableFactAttribute;
using TheoryAttribute = Xunit.SkippableTheoryAttribute;

namespace StrataLint.Cache.Tests;

public sealed class RegisteredCachePathTests
{
    [Fact]
    public void NativeArchivePathsComeOnlyFromTheRegisteredLayer()
    {
        using var fixture = new Fixture();
        string[] paths = [".lake/packages/mathlib/Mathlib", ".lake/packages/mathlib/.lake/build/lib"];
        fixture.Manifest(paths);
        Directory.CreateDirectory(Path.Combine(fixture.Root, ".lake/packages/unregistered"));
        File.WriteAllText(Path.Combine(fixture.Root, ".lake/packages/unregistered/large.setup.json"), "unselected");
        var result = fixture.Keys("dependency");
        Assert.True(result.Exit == 0, result.Text);
        Assert.Equal(string.Join('\n', paths.Order(StringComparer.Ordinal)), ReadOutput(result.Output, "dependency_archive_path"));
        Assert.Equal(".lake/packages", ReadOutput(result.Output, "dependency_path"));
        Assert.DoesNotContain("project_archive_path", result.Output, StringComparison.Ordinal);
    }

    [Fact]
    public void ProducerResumeIncludesRegisteredProjectAndDependencyPaths()
    {
        using var fixture = new Fixture();
        fixture.Manifest([".lake/packages/mathlib"]);
        File.WriteAllText(Path.Combine(fixture.Root, "lakefile.toml"), "name = \"fixture\"\n");
        File.WriteAllText(Path.Combine(fixture.Root, "lean-report-inputs.json"),
            """{"report_execution":{"environment":[]}}""");
        var result = fixture.Command("resume-keys");
        Assert.True(result.Exit == 0, result.Text);
        Assert.Equal(".lake/build/lib\n.lake/packages/mathlib", ReadOutput(result.Output, "resume_archive_path"));
        Assert.StartsWith("lean-producer-resume-v1-", ReadOutput(result.Output, "resume_key"), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("missing-file")]
    [InlineData("unregistered-file")]
    [InlineData("missing-layer")]
    [InlineData("duplicate-layer")]
    [InlineData("unknown-layer")]
    [InlineData("unsupported-schema")]
    [InlineData("empty-paths")]
    [InlineData("duplicate-paths")]
    [InlineData("wrong-root")]
    [InlineData("parent-path")]
    [InlineData("absolute-path")]
    [InlineData("newline")]
    public void InvalidRegistrationCannotBecomeAnOptionalCacheMiss(string defect)
    {
        using var fixture = new Fixture();
        fixture.Manifest([".lake/packages/mathlib"]);
        switch (defect)
        {
            case "missing-file": File.Delete(fixture.ManifestPath); break;
            case "unregistered-file": File.WriteAllText(Path.Combine(fixture.Root, "Meta/FILEMAP.toml"), "files = []\n"); break;
            case "missing-layer": fixture.Raw("{\"schema_version\":1,\"layers\":{\"project\":[\".lake/build/lib\"]}}"); break;
            case "duplicate-layer": fixture.Raw("{\"schema_version\":1,\"layers\":{\"dependency\":[\".lake/packages/mathlib\"],\"dependency\":[\".lake/packages/other\"]}}"); break;
            case "unknown-layer": fixture.Raw("{\"schema_version\":1,\"layers\":{\"guess\":[\".lake/packages/mathlib\"]}}"); break;
            case "unsupported-schema": fixture.Raw("{\"schema_version\":2,\"layers\":{\"dependency\":[\".lake/packages/mathlib\"]}}"); break;
            case "empty-paths": fixture.Manifest([]); break;
            case "duplicate-paths": fixture.Manifest([".lake/packages/mathlib", ".lake/packages/mathlib"]); break;
            case "wrong-root": fixture.Manifest([".lake/build"]); break;
            case "parent-path": fixture.Manifest([".lake/packages/../outside"]); break;
            case "absolute-path": fixture.Manifest(["/tmp/material"]); break;
            case "newline": fixture.Manifest([".lake/packages/mathlib\nother=value"]); break;
        }
        var result = fixture.Keys("dependency");
        Assert.True(result.Exit == 2, result.Text);
        Assert.Contains("cache path registration", result.Text, StringComparison.Ordinal);
        Assert.Empty(result.Output);
    }

    [Fact]
    public void ArchiveLayoutChangesDoNotCreateAnotherMathlibPartition()
    {
        using var fixture = new Fixture();
        fixture.Manifest([".lake/packages/mathlib"]);
        var before = fixture.Keys("dependency");
        fixture.Manifest([".lake/packages/mathlib", ".lake/packages/batteries", ".lake/packages/aesop"]);
        var after = fixture.Keys("dependency");
        Assert.True(before.Exit == 0 && after.Exit == 0, before.Text + after.Text);
        foreach (var key in new[] { "mathlib_revision", "partition", "dependency_key", "dependency_restore_prefix" })
            Assert.Equal(ReadOutput(before.Output, key), ReadOutput(after.Output, key));
        Assert.NotEqual(ReadOutput(before.Output, "dependency_archive_path"), ReadOutput(after.Output, "dependency_archive_path"));
        fixture.Manifest([".lake/packages/aesop", ".lake/packages/batteries", ".lake/packages/mathlib"]);
        var reordered = fixture.Keys("dependency");
        Assert.True(reordered.Exit == 0, reordered.Text);
        Assert.Equal(ReadOutput(after.Output, "dependency_archive_path"), ReadOutput(reordered.Output, "dependency_archive_path"));
    }

    [Theory]
    [InlineData("project")]
    [InlineData("judge")]
    [InlineData("engineering")]
    [InlineData("current")]
    public void RemovedLayersCannotBeSelected(string layer)
    {
        using var fixture = new Fixture();
        var result = fixture.Keys(layer);
        Assert.Equal(2, result.Exit);
        Assert.Contains("invalid choice", result.Text, StringComparison.Ordinal);
        Assert.Empty(result.Output);
    }

    private static string ReadOutput(string commands, string key)
    {
        var lines = commands.Split('\n');
        for (var index = 0; index < lines.Length; index++)
        {
            if (lines[index].StartsWith(key + "=", StringComparison.Ordinal)) return lines[index][(key.Length + 1)..];
            if (!lines[index].StartsWith(key + "<<", StringComparison.Ordinal)) continue;
            var delimiter = lines[index][(key.Length + 2)..];
            return string.Join('\n', lines.Skip(index + 1).TakeWhile(line => line != delimiter));
        }
        Assert.Fail("Missing output: " + key);
        return "";
    }

    private sealed class Fixture : IDisposable
    {
        private readonly TemporaryDirectory temporary = new();
        internal string Root => temporary.Path;
        internal string ManifestPath => Path.Combine(Root, "Meta/ci-cache-paths.json");
        internal Fixture()
        {
            Directory.CreateDirectory(Path.Combine(Root, "Meta"));
            File.WriteAllText(Path.Combine(Root, "lake-manifest.json"), "{\"packages\":[{\"name\":\"mathlib\",\"rev\":\"" + new string('a', 40) + "\"}]}");
            File.WriteAllText(Path.Combine(Root, "lean-toolchain"), "leanprover/lean4:v4.33.0\n");
            File.WriteAllText(Path.Combine(Root, "Meta/FILEMAP.toml"), "files = [{pattern = \"Meta/ci-cache-paths.json\", consumed_by = [\"automation\"]}]\n");
        }
        internal void Manifest(string[] paths) => Raw(JsonSerializer.Serialize(new
        {
            schema_version = 1, layers = new { dependency = paths, project = new[] { ".lake/build/lib" } }
        }));
        internal void Raw(string value) => File.WriteAllText(ManifestPath, value);
        internal (int Exit, string Text, string Output) Keys(string layer) => Command("keys", "--layers", layer);

        internal (int Exit, string Text, string Output) Command(string command, params string[] args)
        {
            var output = Path.Combine(Root, "outputs");
            File.Delete(output);
            var script = Path.Combine(TestRepositoryLayout.FindRoot(), "tools/scripts/worktree/lean_actions.py");
            var result = EngineeringProcess.Process(Root, "env", ["GITHUB_RUN_ID=17", "GITHUB_RUN_ATTEMPT=1", "GITHUB_OUTPUT=" + output,
                "GITHUB_REPOSITORY=owner/repository", "GITHUB_EVENT_NAME=push", "GITHUB_REF=refs/heads/dev",
                "GITHUB_REF_PROTECTED=true", "STRATALINT_CACHE_WRITES=true",
                "python3", "-B", script, command, "--repository", Root, .. args],
                hangGuard: TestBudgets.WorkflowProcessHangGuard, maximumOutputBytes: 1024 * 1024);
            return (result.Exit, result.Text,
                File.Exists(output) ? File.ReadAllText(output) : "");
        }
        public void Dispose() => temporary.Dispose();
    }
}
