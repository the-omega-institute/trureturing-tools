using System.Collections.Immutable;
using System.Formats.Tar;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;

namespace StrataLint.Engine;

// Repository revisions own the release pin. Historical sources always come from the
// archive authenticated by that revision; the working directory may hold another release.
internal sealed class ToolsRelease
{
    internal const string PinPath = "Meta/tools-release.json";
    internal sealed record Member(string Mode, ImmutableArray<byte> Bytes, byte[] Hash);
    internal IReadOnlyDictionary<string, Member> Files { get; }
    private ToolsRelease(Dictionary<string, Member> files) => Files = files;

    // One invocation may compare a base and a candidate repeatedly. Keep only the
    // most recent pair, addressed by the SHA-256 of their complete compressed bytes.
    private static readonly object CacheLock = new();
    private static readonly Dictionary<string, ToolsRelease> Cache = new(StringComparer.Ordinal);

    internal static ToolsRelease? Current(string root)
    {
        var path = Path.Combine(root, PinPath);
        return File.Exists(path) ? Read(root, File.ReadAllBytes(path)) : null;
    }

    private static ToolsRelease Read(string root, ReadOnlyMemory<byte> pin)
    {
        using var document = JsonDocument.Parse(pin);
        var digest = document.RootElement.GetProperty("sha256").GetString();
        if (digest is not { Length: 64 } || digest.Any(ch => !char.IsAsciiHexDigitLower(ch)))
            throw new InvalidDataException($"invalid SHA-256 in {PinPath}");
        lock (CacheLock)
        {
            if (Cache.TryGetValue(digest, out var cached)) return cached;
            var path = Path.Combine(root, "build", "tools-releases", digest + ".tar.gz");
            using var archive = File.OpenRead(path);
            if (Convert.ToHexStringLower(SHA256.HashData(archive)) != digest)
                throw new InvalidDataException($"tools release digest mismatch: {path}");
            archive.Position = 0;
            using var gzip = new GZipStream(archive, CompressionMode.Decompress);
            using var tar = new TarReader(gzip);
            var files = new Dictionary<string, Member>(StringComparer.Ordinal);
            while (tar.GetNextEntry() is { } entry)
            {
                if (entry.EntryType == TarEntryType.Directory) continue;
                if (entry.EntryType is not (TarEntryType.RegularFile or TarEntryType.V7RegularFile)
                    || !entry.Name.StartsWith("tools/", StringComparison.Ordinal)
                    || !RepoPath.TryCreate(entry.Name, out _))
                    throw new InvalidDataException($"invalid tools release member: {entry.Name}");
                var bytes = new byte[checked((int)entry.Length)];
                entry.DataStream?.ReadExactly(bytes);
                var mode = (entry.Mode & UnixFileMode.UserExecute) != 0 ? "100755" : "100644";
                files.Add(entry.Name, new(mode, ImmutableCollectionsMarshal.AsImmutableArray(bytes), SHA256.HashData(bytes)));
            }
            if (files.Count == 0) throw new InvalidDataException("empty tools release");
            if (Cache.Count >= 2) Cache.Remove(Cache.Keys.First());
            return Cache[digest] = new ToolsRelease(files);
        }
    }

    internal static IEnumerable<(string Path, RawChangeKind Kind)> Changes(
        RawRepositorySnapshot previous, RawRepositorySnapshot current)
    {
        const string prefix = "tools/";
        var oldFiles = previous.Entries.Where(entry => entry.Path.StartsWith(prefix, StringComparison.Ordinal))
            .ToDictionary(entry => entry.Path, StringComparer.Ordinal);
        var newFiles = current.Entries.Where(entry => entry.Path.StartsWith(prefix, StringComparison.Ordinal))
            .ToDictionary(entry => entry.Path, StringComparer.Ordinal);
        var oldModes = previous.PathInventory.ToDictionary(entry => entry.Path, entry => entry.EffectiveMode, StringComparer.Ordinal);
        var newModes = current.PathInventory.ToDictionary(entry => entry.Path, entry => entry.EffectiveMode, StringComparer.Ordinal);
        foreach (var path in oldFiles.Keys.Union(newFiles.Keys, StringComparer.Ordinal))
        {
            if (!oldFiles.TryGetValue(path, out var oldFile))
                yield return (path, RawChangeKind.Added);
            else if (!newFiles.TryGetValue(path, out var newFile))
                yield return (path, RawChangeKind.Deleted);
            else if (oldModes[path] != newModes[path] || !oldFile.Bytes.AsSpan().SequenceEqual(newFile.Bytes.AsSpan()))
                yield return (path, RawChangeKind.Modified);
        }
    }

    internal static RawRepositorySnapshot AtRevision(string root, RawRepositorySnapshot snapshot,
        Func<string, bool> select, Func<string, bool> readContents,
        Action<string, ReadOnlyMemory<byte>>? observeContentDigest = null)
    {
        var pin = snapshot.Entries.FirstOrDefault(entry => entry.Path == PinPath);
        if (pin is null) return snapshot;
        if (snapshot.Entries.Any(entry => entry.Path.StartsWith("tools/", StringComparison.Ordinal)))
            throw new InvalidDataException("revision contains both tracked tools and a tools release pin");
        var release = Read(root, pin.Bytes.AsMemory());
        var entries = snapshot.Entries.Where(entry => entry.Path != PinPath || select(entry.Path))
            .Select(entry => entry.Path == PinPath && !readContents(PinPath)
                ? entry with { Bytes = [], ContentWasRead = false } : entry).ToList();
        var inventory = snapshot.PathInventory.IsDefault ? null : snapshot.PathInventory
            .Where(entry => entry.Path != PinPath || select(entry.Path)).ToList();
        foreach (var (path, file) in release.Files.Where(pair => select(pair.Key)))
        {
            var read = readContents(path);
            entries.Add(new(path, read ? file.Bytes : [], ContentWasRead: read));
            inventory?.Add(new(path, null, "regular", file.Mode, null));
            observeContentDigest?.Invoke(path, file.Hash);
        }
        return RawRepositorySnapshot.Create(entries.OrderBy(entry => entry.Path, StringComparer.Ordinal),
            inventory is null ? default : [.. inventory.OrderBy(entry => entry.Path, StringComparer.Ordinal)]);
    }
}
