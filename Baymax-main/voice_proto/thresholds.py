import csv, sys
rows = list(csv.DictReader(open(sys.argv[1] if len(sys.argv) > 1 else "results.csv")))
for length in ("full", "3s", "2s", "1s"):
    r = [x for x in rows if x["length"] == length]
    print(f"\n{length}  ({len(r)} clips)")
    print(" cutoff   named right   WRONG name   'not sure'")
    for t in (0.25, 0.30, 0.35, 0.40, 0.45, 0.50):
        right = wrong = 0
        for x in r:
            best = float(x["true_score"]) if x["correct"] == "True" else float(x["best_wrong"])
            if best >= t:
                right += x["correct"] == "True"
                wrong += x["correct"] != "True"
        print(f"  {t:.2f}      {right/len(r):6.0%}       {wrong/len(r):6.0%}      {(len(r)-right-wrong)/len(r):6.0%}")