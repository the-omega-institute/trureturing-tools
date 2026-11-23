module
public import Mathlib.Data.Nat.Prime.Basic
public import OmegaMath.FrozenDependencyFixture

namespace FrozenFixture

private theorem hidden : True :=
  Exists.elim (show ∃ n : Nat, n.Prime from ⟨2, Nat.prime_two⟩) (fun _ _ => True.intro)

public theorem mathlibHelper : True := hidden
public theorem omegaHelper : Claim := claim

end FrozenFixture
