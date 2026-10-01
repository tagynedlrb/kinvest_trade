import asyncio
from unittest.mock import AsyncMock

import pytest

from kinvest_trade.client import KisRestClient


@pytest.mark.parametrize("method", ["get_domestic_volume_rank", "get_domestic_fluctuation_rank"])
def test_rank_change_rate_keeps_decimal_and_short_code(method):
    client = object.__new__(KisRestClient)
    client._request = AsyncMock(return_value={"output": [{"stck_shrn_iscd": "0167A0", "hts_kor_isnm": "ETF", "prdy_ctrt": "-5.33", "stck_prpr": "10000", "acml_vol": "1000000"}]})
    rows = asyncio.run(getattr(client, method)(top_n=30))
    assert rows[0]["stock_code"] == "0167A0"
    assert rows[0]["change_rate"] == -5.33
    args, kwargs = client._request.call_args
    if method == "get_domestic_fluctuation_rank":
        assert args == ("GET", "/uapi/domestic-stock/v1/ranking/fluctuation", "FHPST01700000")
        params = kwargs["params"]
        assert params["FID_COND_SCR_DIV_CODE"] == "20170"
        assert params["FID_INPUT_CNT_1"] == "30"
        assert {"FID_PRC_CLS_CODE", "FID_TRGT_CLS_CODE", "FID_TRGT_EXLS_CLS_CODE", "FID_DIV_CLS_CODE", "FID_RSFL_RATE1", "FID_RSFL_RATE2"} <= params.keys()
