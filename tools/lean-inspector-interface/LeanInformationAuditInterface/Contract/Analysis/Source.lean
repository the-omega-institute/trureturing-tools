import LeanInformationAuditInterface.Contract.Core
import D5.S3.ConceptDynamics.InformationEscape.DependentFamily

namespace LeanInformationAudit.Analysis
open D5.S3.ConceptDynamics.InformationEscape
open DependentFamily
universe u t s r o a

/-- The exact constructor uses the same type for the original and reconstructed
statements. -/
inductive Reconstruction (original : Sort u) : Sort u → Type u where
  | exact : Reconstruction original original

/-- An author model of the complete original declaration. `occurrence` retains
its original type and universes; callers use the unapplied `@` reference.
`rebuild actual` must reconstruct that type, including all dependent binders.
The selection is a compiler coordinate map, not evidence by itself. -/
structure Source {Statement : Sort u} (occurrence : Statement) where
  signature : Signature.{t,s,r,o,a}
  actual : Realization signature
  rebuild : Realization signature → Sort u
  reconstruction : Reconstruction Statement (rebuild actual)
  selection : Contract.SourceSelection

/-- Reuse ordinary registration operands and their full law, without changing
any ordinary registration obligation or assigning an audit status. -/
def Source.ofRegistration {P : Prop} (occurrence : P)
    {arena : DependentFamily.Arena.{t,s,r,o,a}}
    (registration : DependentFamily.Registration arena P)
    (reconstruction : Reconstruction P (arena.Law registration.actual))
    (selection : Contract.SourceSelection) : Source occurrence where
  signature := arena.signature
  actual := registration.actual
  rebuild := arena.Law
  reconstruction := reconstruction
  selection := selection

end LeanInformationAudit.Analysis
