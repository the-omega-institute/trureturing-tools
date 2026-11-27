module
prelude
public import OmegaInit.Prelude

@[expose] public section

namespace FrozenFixture

def Claim : Prop := Omega.True

theorem claim : Claim := Omega.True.intro

end FrozenFixture
