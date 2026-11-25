import Lake.Build.Common

open Lean Lake

/-- Recover the hash of the nested input tree serialized by Lake. -/
private partial def inputHash (inputs : Array (String × Json)) : Except String Hash := do
  let mut result := Hash.nil
  for (_, value) in inputs do
    let hash ← match value with
      | .arr _ => inputHash (← fromJson? value)
      | .str _ => fromJson? value
      | _ => throw "invalid Lake input trace"
    result := result.mix hash
  return result

private def checkTrace (path : String) : IO Unit := do
  let document ← IO.ofExcept <| Json.parse (← IO.FS.readFile path)
  let metadata : BuildMetadata ← IO.ofExcept <| fromJson? document
  unless document == toJson metadata do throw <| IO.userError s!"incomplete or incompatible Lake trace: {path}"
  if metadata.synthetic then
    unless metadata.inputs.isEmpty do throw <| IO.userError s!"invalid synthetic trace: {path}"
  else if metadata.inputs.isEmpty || (← IO.ofExcept <| inputHash metadata.inputs) != metadata.depHash then
    throw <| IO.userError s!"Lake input trace differs from its dependency hash: {path}"

def main : IO Unit := do
  let request ← IO.ofExcept <| Json.parse (← (← IO.getStdin).readToEnd)
  let files : Array String ← IO.ofExcept <| request.getObjValAs? _ "files"
  let traces : Array String ← IO.ofExcept <| request.getObjValAs? _ "traces"
  let localTraces : Array String ← IO.ofExcept <| request.getObjValAs? _ "localTraces"
  for path in traces do checkTrace path
  for path in files do IO.println (← computeBinFileHash path).hex
  for path in localTraces do
    try
      checkTrace path
      IO.println "valid"
    catch _ => IO.println "invalid"
