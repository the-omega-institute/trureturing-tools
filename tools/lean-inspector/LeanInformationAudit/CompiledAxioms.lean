import Lean.Environment
import Lean.Util.CollectAxioms

namespace LeanInformationAudit.CompiledAxioms
open Lean

/-- Single owner of dependency semantics for both axiom closure and structural
extraction. Keep the type/constructor and optional value halves separate. -/
def declarationDependencyParts (info : ConstantInfo) : Array Name × Option (Array Name) :=
  let types := match info with
    | .quotInfo _ => #[]
    | .inductInfo value => value.type.getUsedConstants ++ value.ctors.toArray
    | value => value.type.getUsedConstants
  (types, (info.value? (allowOpaque := true)).map Expr.getUsedConstants)

/-- The union consumed by the report's transitive axiom traversal. -/
def declarationDependencies (info : ConstantInfo) : Array Name :=
  let (types, values) := declarationDependencyParts info
  types ++ values.getD #[]

/-- Report-shared state for axiom-closure collection. `closure` memoizes the final
sorted axiom set of every constant once its strongly connected component has been
resolved; the remaining fields are the working state of the Tarjan traversal. -/
structure AxiomClosureState where
  counter : Nat := 0
  index : NameMap Nat := {}
  low : NameMap Nat := {}
  onStack : NameSet := {}
  stack : Array Name := #[]
  closure : NameMap (Array Name) := {}

/-- Finalize the complete transitive axiom set of each strongly connected
component. Mutual definitions and inductives share their component's own axioms
and every finalized external successor's closure. -/
private partial def strongConnect (find : Name → Option ConstantInfo)
    (dependencies : ConstantInfo → Array Name)
    (seed : ConstantInfo → IO (Option Name))
    (state : IO.Ref AxiomClosureState) (constant : Name) : IO Unit := do
  state.modify fun s => { s with
    index := s.index.insert constant s.counter
    low := s.low.insert constant s.counter
    counter := s.counter + 1
    onStack := s.onStack.insert constant
    stack := s.stack.push constant }
  let some info := find constant
    | throw <| IO.userError s!"raw.incomplete_closure:{constant}"
  for dependency in dependencies info do
    let s ← state.get
    if s.closure.contains dependency then continue
    if !(s.index.contains dependency) then
      strongConnect find dependencies seed state dependency
      let s ← state.get
      let lowDependency := (s.low.find? dependency).getD 0
      if lowDependency < (s.low.find? constant).getD 0 then
        state.modify fun s => { s with low := s.low.insert constant lowDependency }
    else if s.onStack.contains dependency then
      let indexDependency := (s.index.find? dependency).getD 0
      if indexDependency < (s.low.find? constant).getD 0 then
        state.modify fun s => { s with low := s.low.insert constant indexDependency }
  let s ← state.get
  if (s.low.find? constant).getD 0 == (s.index.find? constant).getD 0 then
    let mut members : Array Name := #[]
    let mut remaining := s.stack
    let mut popped := Name.anonymous
    repeat
      popped := remaining.back!
      remaining := remaining.pop
      members := members.push popped
    until popped == constant
    let memberSet : NameSet := members.foldl (init := {}) (fun set name => set.insert name)
    let mut facts : NameSet := {}
    for member in members do
      let some info := find member
        | throw <| IO.userError s!"raw.incomplete_closure:{member}"
      if let some value ← seed info then
        facts := facts.insert value
      for dependency in dependencies info do
        if !(memberSet.contains dependency) then
          for entry in ((s.closure.find? dependency).getD #[]) do
            facts := facts.insert entry
    let result := facts.toArray
    state.modify fun s => { s with
      closure := members.foldl (init := s.closure) (fun map name => map.insert name result)
      onStack := members.foldl (init := s.onStack) (fun set name => set.erase name)
      stack := remaining }

/-- Combine per-constant facts across the dependency graph, including cycles. Each
interpretation of the graph uses its own cache. -/
def collectClosure (find : Name → Option ConstantInfo)
    (dependencies : ConstantInfo → Array Name) (seed : ConstantInfo → IO (Option Name))
    (state : IO.Ref AxiomClosureState) (constant : Name) : IO (Array Name) := do
  if let some cached := (← state.get).closure.find? constant then
    return cached
  strongConnect find dependencies seed state constant
  return ((← state.get).closure.find? constant).getD #[]

/-- Transitive axiom closure using the report's dependency relation. -/
def collectAxiomsShared (find : Name → Option ConstantInfo)
    (state : IO.Ref AxiomClosureState) (constant : Name) : IO (Array Name) :=
  collectClosure find declarationDependencies
    (fun info => pure (if info matches .axiomInfo _ then some info.name else none)) state constant

end LeanInformationAudit.CompiledAxioms
