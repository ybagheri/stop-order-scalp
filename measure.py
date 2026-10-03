import MetaTrader5 as mt5
mt5.initialize()
mt5.symbol_select("BITCOIN", True)
i = mt5.symbol_info("BITCOIN")
for f in ("digits", "point", "trade_tick_size", "trade_tick_value", "trade_contract_size",
          "volume_min", "volume_max", "volume_step", "trade_stops_level", "trade_freeze_level"):
    print(f, getattr(i, f))