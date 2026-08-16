import json
d=json.load(open("forecasts.json",encoding="utf-8"))
print("generated:",d["generated_at"],"| failed:",d["failed"])
for c in d["companies"]:
    print(f"\n=== {c['ticker']}  {c['period']} ===")
    for label,val in c["values"].items():
        m=c["methods"][label]; w=c["warnings"].get(label,[])
        print(f"  {label:<46} {val:>12,.4g}   [{m}]")
        for x in w[:2]: print(f"      ! {x[:110]}")
    if c["identity_issues"]:
        for i in c["identity_issues"]: print(f"  IDENTITY: {i[:130]}")
