from avws.registry import get_metric
from avws.table_facts import harvest
for key in ["ADI:Adjusted gross margin","ADI:Revenue","HAS:Net fees","DE:Production & Precision Ag operating profit","HD:Comparable sales, total company"]:
    m = get_metric(key)
    fs = harvest(m)
    print(f"\n=== {key}: {len(fs)} table facts ===")
    for f in fs[:6]:
        print(f"  {f.basis:<10} {f.period:<10} {f.value:>10.4g}  | {f.source_quote[:95]}")
