using StrataLint.Engine;

namespace StrataLint.Scribe.Tests;

public sealed class GeneratedArtifactInventoryTests
{
    [Fact]
    public void InventoryProducersAgreeWithActualEmittersAndArtifactIdsRemainStable()
    {
        Assert.Equal(nameof(ScribeEmitter), GeneratedArtifactInventory.DocumentProducer);
        Assert.Equal(nameof(FileMapEmitter), GeneratedArtifactInventory.FileMap.Producer);
        Assert.Equal(nameof(ScribeEmitter), GeneratedArtifactInventory.ScribeAttestation.Producer);
        Assert.Equal(
            ["A-FILEMAP", "A-SCRIBE"],
            GeneratedArtifactInventory.Create([]).Select(item => item.ArtifactId).Order(StringComparer.Ordinal));
    }
}
