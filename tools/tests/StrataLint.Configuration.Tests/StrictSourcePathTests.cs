using System.Text;
using StrataLint.Engine;
using StrataLint.TestSupport;
using Xunit;

namespace StrataLint.Configuration.Tests;

public sealed class StrictSourcePathTests
{
    [Theory]
    [InlineData("OmegaMathlib.lean", "OmegaMathlib.lean", true)]
    [InlineData("OmegaMathlib/**/*.lean", "OmegaMathlib/Data/Nat/Choose/Basic.lean", true)]
    [InlineData("OmegaMathlib/**", "OmegaMathlib/Data/Basic.olean", false)]
    [InlineData("OmegaMathlibExtra/**/*.lean", "OmegaMathlibExtra/Data/Basic.lean", false)]
    [InlineData("strict/**/*.lean", "strict/OmegaInit.lean", true)]
    [InlineData("strict/**/*.lean", "strict/OmegaMathlib/Logic/Function/Defs.lean", true)]
    [InlineData("strict/**/*.lean", "strict/OmegaInit/Prelude.md", false)]
    [InlineData("strict/**", "strict/OmegaInit/Prelude.olean", false)]
    [InlineData("strict/tool.py", "strict/tool.py", false)]
    [InlineData("other/**/*.lean", "other/OmegaInit.lean", false)]
    public void OnlyRegisteredLeanSourcesAreAdmitted(string pattern, string value, bool admitted)
    {
        var policy = LoadPolicy(pattern);
        var path = RepoPath.CreateKnown(value);

        Assert.Equal(admitted, RepositoryPathPolicy.Validate(path, policy) is null);
        Assert.False(RepositoryPathPolicy.TryResolve(path, out _));
    }

    [Theory]
    [InlineData("program", "content", "lean-build")]
    [InlineData("truth", "judge", "lean-build")]
    [InlineData("truth", "content", "developer")]
    public void TheRegistrationMustDescribeLeanVerifiedTruth(string kind, string plane, string verifier)
    {
        var policy = LoadPolicy("strict/**/*.lean", kind, plane, verifier);

        Assert.NotNull(RepositoryPathPolicy.Validate(RepoPath.CreateKnown("strict/OmegaInit.lean"), policy));
    }

    private static ValidatedPolicy LoadPolicy(
        string pattern, string kind = "truth", string plane = "content", string verifier = "lean-build")
    {
        var entry = $"""
            [[files]]
            pattern = "{pattern}"
            kind = "{kind}"
            admission_plane = "{plane}"
            produced_by = "none"
            consumed_by = ["Lean"]
            verified_by = ["{verifier}"]
            artifact_id = "none"
            runtime_disposition = "committed-source"
            """ + "\n";
        var nextEntry = string.CompareOrdinal(pattern, "Papers/**") < 0
            ? "[[files]]\npattern = \"Papers/**\"" : "[[files]]\npattern = \"tools/**\"";
        var manifest = TestFileMap.Canonical.Replace(nextEntry, entry + nextEntry, StringComparison.Ordinal);
        return PolicyLoadAssert.Accepted(RepositoryPolicyLoader.Load(
            Encoding.UTF8.GetBytes(manifest), Encoding.UTF8.GetBytes(TestFileMap.Domains))).Policy;
    }
}
