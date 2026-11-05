"""Separate preregistered INF31 HP reassessment using the EXP-01 lane."""

import pytest

from tests.integration.exp01_support import INF31_REASSESSMENT
from tests.integration.test_exp01_first_batch import CONTROLS, hp_control


@pytest.mark.parametrize("control", CONTROLS)
def test_inf31_hp(b0, tmp_path, control):
    hp_control(b0, tmp_path, "INF31:" + control, control, protocol_path=INF31_REASSESSMENT)
