# Session 2026-02-23: AI Agent Contracts

## Summary

Created five AI agent contracts in `.windsurf/contracts/` to codify hard-won lessons, system constraints, and failure modes into reusable guardrails for future development sessions.

## Contracts Created

| Contract | Focus |
|----------|-------|
| `DATA_CONTRACT.md` | Schema authority (`data_dictionary.json`), HDF5 conventions, consistency rules CR-1–CR-7, field semantic pitfalls, L0–L3 product catalog |
| `METROLOGY_CONTRACT.md` | TickEdgeDetector as sole timing source, RTP timestamp authority, dual Kalman (TSL1/TSL2), search windows, station-specific handling, phase continuity |
| `PHYSICS_CONTRACT.md` | Honest product status (dTEC ✅, group-delay TEC ❌, VTEC ❌), propagation model hierarchy, carrier-phase dTEC as primary science product, reanalysis MUF constraints |
| `WEB_API_CONTRACT.md` | FastAPI + Chart.js + vanilla CSS stack, services-layer data access, numpy type casting, nav consistency, loading/error states, quality honesty in UI |
| `INSTALLATION_CONTRACT.md` | FHS directory layout, git repo vs production split, `update-production.sh` workflow, service ordering, CPU affinity, state persistence |

## Contract Structure

Each contract follows a four-element structure:

1. **Goal** — performance objectives, deliverable products, concrete verification steps
2. **Constraints** — dependencies, rules, boundaries, technology choices
3. **Format** — schemas, logging standards, display conventions, DTOs
4. **Failure Conditions** — specific anti-patterns grounded in real bugs encountered during development

## Design Rationale

- Contracts are **living documents** — they evolve as the system does
- Failure conditions encode **actual bugs we've hit** (HDF5 locking, phase jumps, silent exception swallowing, numpy serialization, etc.)
- Contracts serve as guardrails for AI agents, onboarding material for collaborators, and review criteria for code changes

## Next Sessions: HamSCI 2026 Presentation Preparation

Focus areas for upcoming sessions (15-minute presentation):

**Metrology (clear wins):**
- UTC reconstruction demonstration — how and to what degree D_clock recovers UTC
- Dual Chrony feed (TSL1 vs TSL2) comparison against GPS ground truth
- TickEdgeDetector ensemble performance (50–57 ticks/min, ±0.008–2 ms)

**Physics (highest impact evidence):**
- Carrier-phase dTEC — 250K records/day, ~6 mTECU/min sensitivity, TID/flare signatures
- Multipath identification — all-arrivals product, mode timeline visualization
- Doppler shifts — diurnal signatures, ionospheric dynamics
- Phase measurements — carrier phase stability on unambiguous channels

## Files Changed

- `.windsurf/contracts/DATA_CONTRACT.md` (new)
- `.windsurf/contracts/METROLOGY_CONTRACT.md` (new)
- `.windsurf/contracts/PHYSICS_CONTRACT.md` (new)
- `.windsurf/contracts/WEB_API_CONTRACT.md` (new)
- `.windsurf/contracts/INSTALLATION_CONTRACT.md` (new)
- `docs/changes/SESSION_2026_02_23_AI_AGENT_CONTRACTS.md` (new)
