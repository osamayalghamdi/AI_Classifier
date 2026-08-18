"""F1 audit helper — dump every OFFERING-000 candidate pair with sim, verdict,
LLM reason, and both ticket texts. Uses the verifier cache (no new LLM calls
for cached pairs)."""
import sys

sys.path.insert(0, ".")

from ai_classification.shared.store import store
from ai_classification.services.match.suboffering import offering_of, OFFERING_000
from ai_classification.services.cluster.suboffering_cluster import generate_candidates, AUTO_ACCEPT
from ai_classification.services.cluster.verifier import Verifier
import numpy as np


def main(cache_path: str, shuffle_seed: int | None = None):
    store.setup()
    incidents = store.list_incidents()
    pool = [i for i in incidents
            if offering_of((i.get("classification_dict") or {}).get("service", "")) is None]
    pool.sort(key=lambda i: i["id"])
    if shuffle_seed is not None:
        import random
        random.Random(shuffle_seed).shuffle(pool)
    verifier = Verifier(cache_path=cache_path)
    from ai_classification.services.match.suboffering import embed_pure
    embs = np.stack([embed_pure(i.get("title", ""), i.get("description", "")) for i in pool])
    sim = embs @ embs.T
    np.fill_diagonal(sim, -1.0)
    cands = generate_candidates(pool, sim)
    print(f"pool={len(pool)} shuffle_seed={shuffle_seed} candidates={len(cands)}")
    pairs = [(pool[i], pool[j]) for i, j, _ in cands]
    verdicts = verifier.verify_pairs(pairs)
    yes = 0
    for (i, j, s), (a, b), v in zip(cands, pairs, verdicts):
        fm_a = (a.get("classification_dict") or {}).get("failure_mode", "?")
        fm_b = (b.get("classification_dict") or {}).get("failure_mode", "?")
        flag = " <== YES" if v["decision"] == "YES" else ""
        if v["decision"] == "YES":
            yes += 1
        print(f"\nPAIR {a['id'][:6]}~{b['id'][:6]}  sim={s:.3f}  FM {fm_a}/{fm_b}"
              f"  -> {v['decision']}{flag}")
        print(f"  reason: {v['reason']}")
        print(f"  A: {a['title'][:55]} | {a['description'][:90]}")
        print(f"  B: {b['title'][:55]} | {b['description'][:90]}")
    print(f"\nTOTAL candidates={len(cands)} YES={yes} auto_accept={sum(1 for _,_,s in cands if s >= AUTO_ACCEPT)}")
    return yes, len(cands)


if __name__ == "__main__":
    cache = sys.argv[1]
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else None
    raise SystemExit(0 if main(cache, seed)[0] == 0 else 1)
