# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import numpy as np
import pytest
import xarray as xr
from compute_irrigation_probability import CallbackBuilder, LogisticRegression


def test_distinct_ngi_egi_coefficients(monkeypatch: pytest.MonkeyPatch):
    inputs = [object() for _ in range(5)]
    landsat, cloud_mask, ngi, egi, lst = inputs
    rasters = {
        cloud_mask: np.ones((1, 2, 2)),
        ngi: [[[0, 1], [2, 3]]],
        egi: [[[3, 2], [1, 0]]],
        lst: [[[1, 2], [3, 4]]],
    }
    captured = {}

    monkeypatch.setattr(
        "compute_irrigation_probability.load_raster_match",
        lambda raster, _: xr.DataArray(rasters[raster], dims=("band", "y", "x")),
    )

    def capture_coefficients(model: LogisticRegression, _: np.ndarray):
        captured["coef"] = model.coef_
        raise RuntimeError("stop after coefficient capture")

    monkeypatch.setattr(LogisticRegression, "predict_proba", capture_coefficients)

    with pytest.raises(RuntimeError, match="stop after coefficient capture"):
        CallbackBuilder(1.0, 2.0, 3.0, 4.0)()(landsat, ngi, egi, lst, cloud_mask)  # type: ignore

    np.testing.assert_array_equal(captured["coef"], [[1.0, 2.0, 3.0]])
