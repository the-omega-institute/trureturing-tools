module
public import D5.FrozenDependencyFixture.Helper
public import OmegaMath.FrozenDependencyFixture
public import OmegaChecks.ProjectiveOverlap

/-! One consumer with distinct compiled declaration dependency closures. -/

namespace FrozenFixture

private theorem _closedWitness : True := True.intro

-- Proof types here use distinct libraries within a single import environment.
public theorem omegaProof : Claim := omegaHelper
public theorem typeOnly (h : Claim) : Claim := h
public theorem diagonalHalf (n : Omega.Nat) :
    Omega.Ratio.Equivalent (Omega.Projective.kappa n.succ n.succ)
      (Omega.Ratio.value Omega.Nat.one Omega.Nat.one.succ) :=
  OmegaChecks.ProjectiveOverlap.diagonal_half n

public theorem axiomFree : ∀ (p : Prop), p → p := fun _ h => h

-- True occurs in the type, while the imported private proof reaches Mathlib.
public theorem mathlibProof : True := mathlibHelper

public theorem oneAxiom : True = True := propext Iff.rfl

public theorem fullAxioms : ∃ x : True, x = True.intro ∧ True = True
    ∧ Quot.mk (fun (_ _ : True) => True) True.intro = Quot.mk _ True.intro :=
  ⟨Classical.choice ⟨True.intro⟩, Subsingleton.elim _ _, propext Iff.rfl,
    Quot.sound (a := True.intro) (b := True.intro) True.intro⟩

public inductive AxiomFamily where
  | plain
  | dependent (witness : (propext (Iff.rfl : True ↔ True)) = rfl)

end FrozenFixture
