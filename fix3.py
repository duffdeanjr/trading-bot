import os
for root,dirs,files in os.walk("."):
    dirs[:]=[d for d in dirs if d not in ["__pycache__",".git"]]
    for f in files:
        if f.endswith(".py"):
            p=os.path.join(root,f)
            c=open(p,encoding="utf-8").read()
            n=c.replace("alpaca.data","alpaca.data")
            if n!=c:
                open(p,"w",encoding="utf-8").write(n)
                print("fixed",p)
print("done")
