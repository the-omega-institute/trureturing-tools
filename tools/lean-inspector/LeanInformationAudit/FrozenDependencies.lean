import Lean.Util.FindExpr
import LeanInformationAudit.CompiledAxioms
import LeanInformationAudit.RawArtifacts

namespace LeanInformationAudit.FrozenDependencies
open Lean

private def literalTypes (expression : Expr) : Array Name := Id.run do
  let mut result := #[]
  if (expression.find? (· matches .lit (.natVal _))).isSome then
    result := result.push `Nat
  if (expression.find? (· matches .lit (.strVal _))).isSome then
    result := result.push `String
  return result

-- getUsedConstants includes the structure name in a kernel projection.
-- The provenance graph also follows implicit types and kernel reduction rules.
private def dependencies (info : ConstantInfo) : Array Name :=
  let direct := CompiledAxioms.declarationDependencies info
  let literals := literalTypes info.type ++
    ((info.value? (allowOpaque := true)).map literalTypes).getD #[]
  direct ++ literals ++ match info with
  | .ctorInfo constructor => #[constructor.induct]
  | .recInfo recursor => recursor.all.toArray ++ recursor.rules.foldl (init := #[]) fun deps rule =>
      deps.push rule.ctor ++ rule.rhs.getUsedConstants ++ literalTypes rule.rhs
  | .quotInfo quotient => quotient.type.getUsedConstants
  | _ => #[]

def collect (store : RawArtifacts.Store) (cache : IO.Ref CompiledAxioms.AxiomClosureState)
    (constant : Name) : IO (Array Name) :=
  CompiledAxioms.collectClosure store.constants.find? dependencies (fun info => do
    let some owner := store.selectedOwner? info.name
      | throw <| IO.userError s!"frozen.dependencies.missing_owner:{info.name}"
    let root := owner.getRoot
    return if root == `D5 || root == `Reg || root == `OmegaChecks || root == `Trureturing then none else some root
  ) cache constant

end LeanInformationAudit.FrozenDependencies
