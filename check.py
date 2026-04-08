import alpaca.trading.stream as ts 
print([m for m in dir(ts.TradingStream) if not m.startswith('__')]) 
