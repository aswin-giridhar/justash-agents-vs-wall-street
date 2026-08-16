import json
d=json.load(open("forecasts.json",encoding="utf-8"))
print("generated:", d["generated_at"], "| failed:", d["failed"])
order=["HD","ADI","HAS","DE"]
for t in order:
    c=[x for x in d["companies"] if x["ticker"]==t][0]
    print(f"\n{t}  {c['period']}")
    for k,v in c["values"].items():
        print(f"   {k:<46} {v:>13,.4f}   [{c['methods'][k]}]")
