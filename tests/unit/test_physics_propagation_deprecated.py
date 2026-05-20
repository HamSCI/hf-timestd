"""Regression test for P-H12: PhysicsPropagationModel is deprecated.

PhysicsPropagationModel is superseded by propagation_model.HFPropagationModel.
It was still advertised in hf_timestd.core.__all__ with no runtime warning —
and its Tier-2 ionospheric-delay term is ~1e16x too small (a TECU->el/m² unit
error), which is itself reason enough not to use it. It must now emit a
DeprecationWarning on instantiation and no longer appear in the package's
public __all__.
"""

import pytest

import hf_timestd.core as core
from hf_timestd.core.physics_propagation import PhysicsPropagationModel


def test_instantiation_emits_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="HFPropagationModel"):
        PhysicsPropagationModel(
            receiver_lat=40.0, receiver_lon=-95.0,
            enable_iri=False, enable_pylap=False,
        )


def test_deprecated_names_not_in_public_all():
    for name in ("PhysicsPropagationModel", "PropagationResult",
                 "PropagationModelTier"):
        assert name not in core.__all__


def test_class_is_still_importable_for_backward_compat():
    # Dropping it from __all__ must not break an explicit import path.
    from hf_timestd.core.physics_propagation import (  # noqa: F401
        PhysicsPropagationModel as _Model,
    )
    assert _Model is PhysicsPropagationModel
