import os
for fn in os.listdir("alpaca_local"):
    if fn.endswith(".py"):
        p="alpaca_local/"+fn
        c=open(p,encoding="utf-8").read()
        n=c.replace("from alpaca_local.trading","from alpaca.trading").replace("from alpaca.data","from alpaca.data")
        if n!=c:
            open(p,"w",encoding="utf-8").write(n)
            print("fixed",p)
print("done")
