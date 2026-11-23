module
prelude
public import OmegaInit.FrozenDependencyFixture

@[expose] public section

namespace FrozenFixture

def Claim : Prop := Witness

theorem claim : Claim := Witness.intro

end FrozenFixture
