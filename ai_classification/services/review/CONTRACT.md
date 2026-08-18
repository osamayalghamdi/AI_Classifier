# Review Service — CONTRACT

Human-in-the-loop proposal approval/minting + proposal review API. No background work.

## proposal_routes — services/review/proposal_routes.py
- Input: `GET /proposals` (filter: status, offering_id); `POST /proposals/{id}/decision` with `{decision: approve|reject|merge, target_sub_offering_id, note, new_offering_name}`; `GET /proposals/{id}`.
- Output: approve → ACTIVE sub_offering minted (`store.create_sub_offering`) + cluster tickets become exemplars, pool drained; `new_offering_name` mints under a NEW offering (OFFERING-000 path); reject → members get 24h cooldown (`pool_set_cooldown`), stay in pool; merge → members attached as exemplars of an existing sub_offering, pool drained; GET /proposals enriched with member titles.
- Depends on: `core.store` (list_proposals, decide_proposal, create_sub_offering, add_exemplar, pool_remove_many, pool_set_cooldown, get_sub_offering), `core.suboffering.embed_pure`.
- Called by: FastAPI app — router mounted in `api/routes.py` (`include_router`, prefix `/proposals`); UI: frontend/dashboard/review.html.
- Key invariants: one-shot rule — only `pending` proposals decidable, repeat calls idempotent (409 on already-decided); proposals NEVER auto-mint (only this human gate mints); decisions persist via `store.decide_proposal`.
- Entry point: `router` (APIRouter); `_mint_sub_offering(proposal, offering_id=None)` internal mint helper.
