#!/usr/bin/env bash
# Scan every judge-surface file in the candidate inventory with the SL-030 scanner built in that tree,
# exactly as the admission judge would when the rule implementation is in the candidate delta
# (every judge-surface file is re-judged then). Prints each finding; exit 1 if there is any.
#
# Usage: tools/scripts/agent/sl030-real-surface.sh <tree-root>
#
# Build the CLI with `make -C tools dotnet` before checking the real surface.
set -euo pipefail

root=$(cd "${1:?usage: sl030-real-surface.sh <tree-root>}" && pwd)
# The CLI's output directory carries the Engine together with every dependency it loads
# (YamlDotNet, Tomlyn, …); Assembly.LoadFrom resolves siblings from there.
dll=$(ls "$root"/tools/StrataLint.Cli/bin/Release/net*/StrataLint.Engine.dll 2>/dev/null | head -1 || true)
[ -n "$dll" ] || { echo "sl030-real-surface: no built StrataLint.Engine.dll under the CLI output of $root (run make -C tools dotnet)" >&2; exit 64; }

probe=$(mktemp -d "${TMPDIR:-/tmp}/sl030-probe.XXXXXX")
trap 'rm -rf "$probe"' EXIT
cat > "$probe/Probe.csproj" <<'EOF'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
</Project>
EOF
cat > "$probe/Program.cs" <<'EOF'
using System.Collections;
using System.Reflection;

var dll = args[0];
var root = args[1];
var assembly = Assembly.LoadFrom(dll);
var scanner = assembly.GetType("StrataLint.Engine.JudgeSurfaceRevisionScanner")
    ?? throw new InvalidOperationException("JudgeSurfaceRevisionScanner not found");
var isSurface = scanner.GetMethod("IsJudgeSurfacePath", BindingFlags.NonPublic | BindingFlags.Static)
    ?? throw new InvalidOperationException("IsJudgeSurfacePath not found");
var scan = scanner.GetMethod("Scan", BindingFlags.NonPublic | BindingFlags.Static)
    ?? throw new InvalidOperationException("Scan not found");
var inventory = assembly.GetType("StrataLint.Engine.GitIndexRepositoryFiles", throwOnError: true)!
    .GetMethod("EnumerateTracked", BindingFlags.NonPublic | BindingFlags.Static)
    ?? throw new InvalidOperationException("EnumerateTracked not found");
var entries = (IReadOnlyList<(string RelativePath, string Mode)>)inventory.Invoke(null, [root])!;
var files = 0;
var findings = 0;
foreach (var (path, _) in entries)
{
    if (!(bool)isSurface.Invoke(null, new object[] { path })!)
    {
        continue;
    }

    files++;
    var text = File.ReadAllText(Path.Combine(root, path));
    foreach (var message in (IEnumerable)scan.Invoke(null, new object[] { path, text })!)
    {
        findings++;
        Console.WriteLine($"SL-030 {path}: {message}");
    }
}

Console.WriteLine($"REAL_SURFACE_RESULT files={files} findings={findings}");
return findings == 0 ? 0 : 1;
EOF
(cd "$probe" && dotnet build -c Release --nologo -v q >/dev/null)
dotnet "$probe/bin/Release/net10.0/Probe.dll" "$dll" "$root"
