# GTA VI trophy rarity forecaster

Forecasts, for each of the 55 GTA VI trophies, the percent of Exophase-tracked
players who will have earned it at the target time — as five quantiles
(5%, 25%, 50%, 75%, 95%) on the logit scale.

## Model

Work in **counts**, not rates: holders `E = rate * N / 100`, where `N` is
players tracked. When Exophase refreshes and `N` jumps, rates fall by
dilution even though nobody un-earned anything. New joiners bring a low
trophy prevalence (about 1.03 trophies each on launch night, almost all of
it the 100% first trophy), and the existing cohort slowly earns more.
The program pools joiner prevalence and existing-cohort progress across
the history's refresh steps, forecasts `N` at the target with a damped
power-law / recent-growth fit (taking the smaller), forecasts each
trophy's holders, and converts back to a rate.
A flat history (no refresh yet) assumes one launch-night-sized refresh
before the target. See `STRATEGY.md` for the full model and validation.

## Run

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

Stdlib only, no network, deterministic. One file: `main.py`.
The grader runs `python3 -P -s /bot/main.py` with one `<id>.in` in `/input`
and reads `/output/<id>.out`.
