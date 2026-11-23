module
public import Mathlib.Data.Nat.Prime.Basic
public import OmegaMath.FrozenDependencyFixture

namespace FrozenFixture

private theorem hidden : Nat.Prime 2 := Nat.prime_two

public theorem mathlibHelper : ∃ n : Nat, n.Prime := ⟨2, hidden⟩
public theorem omegaHelper : Claim := claim

end FrozenFixture
