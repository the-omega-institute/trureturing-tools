using System.Collections.Immutable;
using StrataLint.Engine;

namespace StrataLint.Scribe;

public static class ScribeCli
{
    public static ImmutableArray<string> ImplementedCommands { get; } =
    [
        "content-check",
        "emit",
        "filemap",
        "lean-inputs",
        "resources",
        "resources release",
        "resources verify-release",
        "scripts",
    ];

    public static int Run(
        IReadOnlyList<string> arguments,
        string workingDirectory,
        TextWriter output,
        TextWriter error) => Run(arguments, workingDirectory, output, error, input: null);

    internal static int Run(
        IReadOnlyList<string> arguments,
        string workingDirectory,
        TextWriter output,
        TextWriter error,
        TextReader? input)
    {
        ArgumentNullException.ThrowIfNull(arguments);
        ArgumentException.ThrowIfNullOrWhiteSpace(workingDirectory);
        ArgumentNullException.ThrowIfNull(output);
        ArgumentNullException.ThrowIfNull(error);

        var command = arguments.Count == 0 ? string.Empty : arguments[0];
        try
        {
            if (arguments is ["lean-inputs", "--affected", "--paths-from", var manifest, "--base", var revision])
                return PullRequestLeanChecks.Plan(FindRepositoryRoot(workingDirectory), ReadPaths(manifest, input), output, revision);
            if (arguments is ["lean-inputs", "--affected", "--paths-from", var changed])
                return PullRequestLeanChecks.Plan(FindRepositoryRoot(workingDirectory), ReadPaths(changed, input), output);
            if (arguments is ["content-check", "--scoped", "--report", var artifact, "--targets-from", var targets,
                "--paths-from", var changedPaths] && !string.IsNullOrWhiteSpace(artifact)
                && !string.IsNullOrWhiteSpace(targets) && !string.IsNullOrWhiteSpace(changedPaths))
                return PullRequestLeanChecks.Check(FindRepositoryRoot(workingDirectory), artifact,
                    ReadPaths(targets, input), ReadPaths(changedPaths, input), output, error);
            if (arguments is ["content-check", "--scoped", "--targets-from", var emptyTargets,
                "--paths-from", var paths] && !string.IsNullOrWhiteSpace(emptyTargets) && !string.IsNullOrWhiteSpace(paths))
                return PullRequestLeanChecks.Check(FindRepositoryRoot(workingDirectory), null,
                    ReadPaths(emptyTargets, input), ReadPaths(paths, input), output, error);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
            or ArgumentException or FormatException or InvalidOperationException or InvalidDataException)
        {
            error.WriteLine(exception.Message);
            return 2;
        }
        if (command == "lean-inputs")
        {
            if (arguments.Count != 3 || arguments[1] != "--paths-from"
                || string.IsNullOrWhiteSpace(arguments[2]))
            {
                error.WriteLine("usage: lean-inputs --paths-from <file|->");
                return 2;
            }
            try
            {
                foreach (var module in ScribeLeanInputs.Select(FindRepositoryRoot(workingDirectory),
                    ReadPaths(arguments[2], input)))
                    output.WriteLine(module);
                return 0;
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                or ArgumentException or FormatException or InvalidOperationException)
            {
                error.WriteLine(exception.Message);
                return 2;
            }
        }
        if (command == "resources")
        {
            return ScribeResourceCommands.Run(arguments, workingDirectory,
                () => FindRepositoryRoot(workingDirectory), output, error);
        }

        if (command == "scripts")
        {
            try
            {
                return ScribeScriptVerifyCommands.Run(
                    arguments,
                    FindRepositoryRoot(workingDirectory),
                    input,
                    output,
                    error);
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                or ArgumentException or FormatException or InvalidOperationException)
            {
                error.WriteLine(exception.Message);
                return 2;
            }
        }

        if (command == "emit" && arguments.Count is >= 3 and <= 5
            && arguments[1] == "--paths-from"
            && !string.IsNullOrWhiteSpace(arguments[2])
            && arguments.Skip(3).All(static option => option is "--check" or "--scoped")
            && arguments.Skip(3).Distinct(StringComparer.Ordinal).Count() == arguments.Count - 3)
        {
            try
            {
                var repositoryRoot = FindRepositoryRoot(workingDirectory);
                var paths = ReadPaths(arguments[2], input);
                ImmutableArray<string>? scopedInputs = arguments.Contains("--scoped")
                    ? ScribeLeanInputs.Select(repositoryRoot, paths)
                    : null;
                if (paths.IsEmpty)
                {
                    output.WriteLine("emitted: 0 changed blueprint(s)");
                    return 0;
                }
                return ScribeEmitter.EmitPaths(
                    repositoryRoot,
                    paths,
                    arguments.Contains("--check"),
                    output,
                    error,
                    () => LeanCompiledArtifactReports.ReadScopedRepositoryFiles(repositoryRoot,
                        (scopedInputs ?? ScribeLeanInputs.Select(repositoryRoot, paths))
                            .Select(static module => module.Replace('.', '/') + ".lean")));
            }
            catch (Exception exception) when (
                exception is IOException or UnauthorizedAccessException or ArgumentException
                    or FormatException or InvalidOperationException or InvalidDataException)
            {
                error.WriteLine(exception.Message);
                return 2;
            }
        }

        if (command == "emit")
        {
            error.WriteLine(arguments.Count == 1 || (arguments.Count == 2 && arguments[1] == "--check")
                ? "MissingPathsManifest: emit requires --paths-from <file|->" : Usage);
            return 2;
        }

        if (arguments is not (["filemap"] or ["filemap", "--check"]))
        {
            error.WriteLine(Usage);
            return 2;
        }

        try
        {
            return FileMapEmitter.Emit(FindRepositoryRoot(workingDirectory), arguments.Count == 2, output, error);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or ArgumentException)
        {
            error.WriteLine(exception.Message);
            return 2;
        }
    }

    private const string Usage =
        "usage: dotnet run --project tools/StrataLint.Scribe -- "
        + "filemap [--check] | lean-inputs [--affected] --paths-from <file|-> [--base <revision>] | emit --paths-from <file|-> [--scoped] [--check] "
        + "| content-check --scoped [--report <file>] --targets-from <file> --paths-from <file> "
        + "| resources pack --out <file> | resources verify --pack <file> "
        + "| resources release --out <directory> "
        + "| resources verify-release --dir <directory> [--total-sha256 <digest>] "
        + "| scripts verify [--paths-from <file|->]";

    /// <summary>
    /// The paths to judge. `-` reads them from standard input, which keeps the change's
    /// paths out of a temporary file the caller would then have to clean up.
    /// </summary>
    private static ImmutableArray<string> ReadPaths(string pathsFile, TextReader? input) =>
        MarkdownFormulaScope.ParsePaths(string.Equals(pathsFile, "-", StringComparison.Ordinal)
            ? (input ?? Console.In).ReadToEnd()
            : File.ReadAllText(pathsFile));

    private static string FindRepositoryRoot(string workingDirectory)
    {
        for (var current = new DirectoryInfo(Path.GetFullPath(workingDirectory));
             current is not null;
             current = current.Parent)
        {
            if (File.Exists(Path.Combine(current.FullName, "global.json"))
                && Directory.Exists(Path.Combine(current.FullName, "Blueprint")))
            {
                return current.FullName;
            }
        }

        throw new DirectoryNotFoundException(
            "Could not locate a repository root containing global.json and Blueprint/.");
    }
}
