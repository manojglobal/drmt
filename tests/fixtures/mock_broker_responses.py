"""Mock API responses for all 6 brokers in DRMT V2."""

FLATTRADE_POSITIONS_LONG = {
    "status": "S",
    "errMsg": "",
    "positionBookArr": [
        {
            "instrument": "RELIANCE-EQ",
            "tsym": "RELIANCE-EQ",
            "token": "2885",
            "exch": "NSE",
            "prd": "I",
            "netqty": 10,
            "daybuyqty": 10,
            "daysellqty": 0,
            "daybuyavgprc": 2900.50,
            "daysellavgprc": 0,
            "netavgprc": 2900.50,
            "ltp": 2915.00,
            "pndl": 145.00,
            "urmtom": 145.00,
            "rpnl": "0.00"
        }
    ]
}

FLATTRADE_POSITIONS_SHORT = {
    "status": "S",
    "errMsg": "",
    "positionBookArr": [
        {
            "instrument": "WIPRO-EQ",
            "tsym": "WIPRO-EQ",
            "token": "3787",
            "exch": "NSE",
            "prd": "I",
            "netqty": -1,
            "daybuyqty": 1,
            "daysellqty": 2,
            "daybuyavgprc": 193.79,
            "daysellavgprc": 193.73,
            "netavgprc": 193.73,
            "ltp": 193.30,
            "pndl": 0.43,
            "urmtom": 0.43,
            "rpnl": "-0.06"
        }
    ]
}

FLATTRADE_POSITIONS_MIXED_WITH_ZERO = {
    "status": "S",
    "errMsg": "",
    "positionBookArr": [
        {
            "instrument": "TCS-EQ",
            "tsym": "TCS-EQ",
            "token": "11536",
            "exch": "NSE",
            "prd": "I",
            "netqty": 0,
            "daybuyqty": 5,
            "daysellqty": 5,
            "daybuyavgprc": 3900.0,
            "daysellavgprc": 3910.0,
            "ltp": 3912.0
        },
        {
            "instrument": "INFY-EQ",
            "tsym": "INFY-EQ",
            "token": "1594",
            "exch": "NSE",
            "prd": "C",
            "netqty": 2,
            "daybuyqty": 2,
            "daysellqty": 0,
            "daybuyavgprc": 1850.0,
            "daysellavgprc": 0,
            "ltp": 1855.0
        }
    ]
}

FLATTRADE_EMPTY_HEALTHY = {
    "status": "S",
    "errMsg": "",
    "positionBookArr": [],
    "segmentCount": []
}

FLATTRADE_EMPTY_STATUS_I = {
    "status": "I",
    "errMsg": "",
    "positionBookArr": [],
    "segmentCount": []
}

FLATTRADE_HOLDINGS_VALID = {
    "status": "S",
    "errMsg": "",
    "holdingsArr": [],
    "holdingsqtyarr": [],
    "showeds": False,
    "mtfholdings": []
}

FLATTRADE_HOLDINGS_EXPIRED = {
    "status": "I",
    "errMsg": "Session Expired :  Invalid Session Key",
    "holdingsArr": [],
    "holdingsqtyarr": [],
    "showeds": False,
    "mtfholdings": []
}

FLATTRADE_BROKER_ERROR = {
    "status": "E",
    "errMsg": "Backend Order Management System Down",
    "positionBookArr": []
}

ALICEBLUE_POSITIONS_SUCCESS = {
    "stat": "Ok",
    "data": [
        {
            "TradingSymbol": "SBIN-EQ",
            "Exchange": "NSE",
            "Product": "MIS",
            "Netqty": 50,
            "buyavgprice": 820.50,
            "Price": 820.50,
            "LTP": 825.00,
            "MtoM": 225.00,
            "Token": "3045"
        }
    ]
}

ALICEBLUE_EC920_NO_POSITIONS = {
    "stat": "Not_Ok",
    "emsg": "EC920: No open positions"
}

ALICEBLUE_AUTH_EXPIRED = {
    "stat": "Not_Ok",
    "emsg": "User session token has expired, please re-authenticate"
}

KOTAK_POSITIONS_SUCCESS = {
    "stat": "Ok",
    "data": [
        {
            "symbol": "HDFCBANK-EQ",
            "exchange": "NSE",
            "product": "MIS",
            "flBuyQty": 10,
            "flSellQty": 0,
            "buyAmt": 16500.0,
            "sellAmt": 0.0,
            "ltp": 1660.0,
            "token": "1333"
        }
    ]
}

KOTAK_AUTH_EXPIRED = {
    "stat": "not_ok",
    "emsg": "Session token invalid or expired"
}

MSTOCK_POSITIONS_SUCCESS = {
    "status": "success",
    "data": [
        {
            "tradingsymbol": "ICICIBANK-EQ",
            "exchange": "NSE",
            "product": "INTRADAY",
            "quantity": 25,
            "avg_price": 1250.0,
            "ltp": 1260.0,
            "pnl": 250.0,
            "instrument_token": "4963"
        }
    ]
}

FYERS_POSITIONS_SUCCESS = {
    "s": "ok",
    "netPositions": [
        {
            "symbol": "NSE:NIFTY26NOV24000CE",
            "instrument_type": "OPTIDX",
            "exchange": "NFO",
            "netQty": 25,
            "buyAvg": 150.0,
            "sellAvg": 0.0,
            "netAvg": 150.0,
            "ltp": 155.0,
            "pl": 125.0,
            "productType": "INTRADAY"
        }
    ]
}

FYERS_AUTH_EXPIRED = {
    "s": "error",
    "code": 401,
    "message": "Token expired or unauthorized"
}

AXIS_POSITIONS_SUCCESS = {
    "status": "success",
    "positions": [
        {
            "scriptId": "11939",
            "exchange": "NSE",
            "product": "INTRADAY",
            "netQty": -5,
            "avgPrice": 193.5,
            "ltp": 192.0,
            "pnl": 7.5
        }
    ]
}
