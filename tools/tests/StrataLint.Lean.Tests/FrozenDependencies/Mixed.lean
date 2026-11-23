module
public import D5.FrozenDependencyFixture.Helper
public import OmegaMath.FrozenDependencyFixture

/-! One consumer with distinct compiled declaration dependency closures. -/

namespace FrozenFixture

private theorem _closedWitness : True := True.intro

-- Proof types here use distinct libraries within a single import environment.
public theorem omegaProof : Claim := omegaHelper
public theorem typeOnly (h : Claim) : Claim := h
public theorem axiomFree : ∀ (p : Prop), p → p := fun _ h => h

-- True occurs in the type, while the imported private proof reaches Mathlib.
public theorem mathlibProof : True := mathlibHelper

public theorem oneAxiom : True = True := propext Iff.rfl

public theorem fullAxioms : ∃ x : True, x = True.intro ∧ True = True
    ∧ Quot.mk (fun (_ _ : True) => True) True.intro = Quot.mk _ True.intro :=
  ⟨Classical.choice ⟨True.intro⟩, Subsingleton.elim _ _, propext Iff.rfl,
    Quot.sound (a := True.intro) (b := True.intro) True.intro⟩

end FrozenFixture
