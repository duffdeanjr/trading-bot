import os
for root,dirs,files in os.walk("."):
    dirs[:]=[d for d in dirs if d!="__pycache__"]
    for f in files:
        if f.endswith(".py"):
            p=os.path.join(root,f)
            c=open(p,encoding="utf-8").read()
            n=c.replace("from alpaca_local import","from alpaca_local import").replace("from alpaca_local.","from alpaca_local.").replace("import alpaca_local.","import alpaca_local.")
            if n!=c:
                open(p,"w",encoding="utf-8").write(n)
                print("updated",p)
print("done")
