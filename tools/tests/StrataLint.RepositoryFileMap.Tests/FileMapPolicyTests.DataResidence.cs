using StrataLint.FileMap;

namespace StrataLint.RepositoryFileMap.Tests;

public sealed partial class FileMapPolicyTests
{
    [Fact]
    public void CommittedDataFixturesRespectTheProgramResidenceBoundary()
    {
        var root = TestRepositoryLayout.FindRoot();
        var manifest = FileMapLoader.LoadRepository(root);
        var release = ToolsRelease.Current(root);
        var findings = FileMapPolicy.InspectDirectoryKinds(manifest, FileMapPolicy.TrackedPaths(root),
            isReleaseMember: release is null ? null : release.Files.ContainsKey);

        Assert.DoesNotContain(findings,
            finding => finding.Code == "FILEMAP-DATA-RESIDENCE");
    }
}
